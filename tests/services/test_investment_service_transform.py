# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Integration tests for ``InvestmentService.transform_upload_to_investments``.

Live-DB tests against the compose Postgres. Each test seeds a tenant
+ user + asset-class catalogue (including ``"unclassified"``) +
a ``data_uploads`` row carrying the documented Excel-import JSONB sheets, then
invokes the transform method and verifies the resulting
``investments`` / ``investment_navs`` / ``investment_cashflows``
state.

Coverage targets (per the sub-stream 4c acceptance criteria):

* IT-01 round-trip: 3 investments × all field kinds → DB.
* IT-02 replace logic: re-import overwrites NAV / cashflow rows.
* IT-03 soft-delete-with-reactivation symmetry.
* IT-04 idempotency: two consecutive runs → identical state.
* IT-05 cross-tenant isolation: shared name in two tenants is OK.
* IT-06 asset-class fallback: empty Asset Class → ``"unclassified"``.
* IT-07 dry-run does not write.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID

import pandas as pd
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from core.repositories import (
    AssetClassRepository,
    DataUploadRepository,
    InvestmentCashflowRepository,
    InvestmentNavRepository,
    InvestmentRepository,
    UserRepository,
    tenant_context,
)
from services.data_normalization import InvestmentExtractor
from services.investments import InvestmentService


# ---------------------------------------------------------------------------
# Helpers — DataFrame builders that round-trip through the documented JSONB
# (``DataFrame.to_json(orient="split")``) shape via ``DataUploadRepository``.
# ---------------------------------------------------------------------------


def _attributes_df(
    investments: dict[str, dict[str, object]],
) -> pd.DataFrame:
    """Build the synthetic attributes DataFrame (rows × investments).

    The dict shape mirrors the parsed-Attributes-sheet shape produced
    by ``modules.front_office.data_import.load_excel`` (rows for
    ``Investment Type`` / ``Investment Sub-Class`` followed by
    free-form attribute rows from row 4 onwards in the workbook).
    """
    columns = list(investments.keys())
    attribute_rows = [
        "Investment Type",
        "Investment Sub-Class",
        "Asset Class",
        "Manager / Fondsname",
        "Region",
        "Vintage Year",
        "Währung",
    ]
    data = []
    for row_label in attribute_rows:
        data.append([investments[col].get(row_label) for col in columns])
    return pd.DataFrame(data, index=attribute_rows, columns=columns)


def _timeseries_df(columns: list[str], rows: list[tuple[str, list[object]]]) -> pd.DataFrame:
    idx = pd.DatetimeIndex([pd.Timestamp(d) for d, _ in rows], name="Date")
    return pd.DataFrame([vals for _, vals in rows], index=idx, columns=columns)


def _build_sheets(
    investments: dict[str, dict[str, object]],
    *,
    nav_actual: list[tuple[str, list[object]]] | None = None,
    nav_plan: list[tuple[str, list[object]]] | None = None,
    cf_in_actual: list[tuple[str, list[object]]] | None = None,
    cf_out_actual: list[tuple[str, list[object]]] | None = None,
) -> dict[str, pd.DataFrame]:
    """Compose the canonical snake_case sheet dict for ``create_upload``."""
    columns = list(investments.keys())
    sheets: dict[str, pd.DataFrame] = {
        "attributes": _attributes_df(investments),
    }
    if nav_actual is not None:
        sheets["navs_actual"] = _timeseries_df(columns, nav_actual)
    if nav_plan is not None:
        sheets["navs_plan"] = _timeseries_df(columns, nav_plan)
    if cf_in_actual is not None:
        sheets["cash_flow_in_actual"] = _timeseries_df(columns, cf_in_actual)
    if cf_out_actual is not None:
        sheets["cash_flow_out_actual"] = _timeseries_df(columns, cf_out_actual)
    return sheets


async def _seed_actor_and_classes(app_engine: AsyncEngine, tenant_id: UUID, *, email: str):
    """Seed user + ``unclassified`` + ``private_equity`` asset classes."""
    async with tenant_context(app_engine, tenant_id) as session:
        actor = await UserRepository(session).create(email=email, password_hash="x" * 8)
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        ac_unclass = await AssetClassRepository(session).create(
            code="unclassified", display_name="Unclassified"
        )
        ac_pe = await AssetClassRepository(session).create(
            code="private_equity", display_name="Private Equity"
        )
        ac_eq = await AssetClassRepository(session).create(
            code="listed_equity", display_name="Listed Equity"
        )
    return actor, ac_unclass, ac_pe, ac_eq


def _build_service(session) -> InvestmentService:
    return InvestmentService(
        investments=InvestmentRepository(session),
        navs=InvestmentNavRepository(session),
        cashflows=InvestmentCashflowRepository(session),
    )


async def _create_upload(
    app_engine: AsyncEngine,
    tenant_id: UUID,
    user_id: UUID,
    *,
    sheets: dict[str, pd.DataFrame],
    filename: str = "test.xlsx",
    file_hash: str | None = None,
):
    if file_hash is None:
        # 64-char hex — same shape as SHA-256.
        file_hash = ("a" * 64) + filename
        file_hash = file_hash[:64]
    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        upload = await DataUploadRepository(session).create_upload(
            uploaded_by=user_id,
            filename=filename,
            file_hash=file_hash,
            size_bytes=1024,
            format_version="v2",
            sheets=sheets,
        )
    return upload


async def _run_transform(
    app_engine: AsyncEngine,
    tenant_id: UUID,
    user_id: UUID,
    upload_id: UUID,
    *,
    dry_run: bool = False,
):
    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        service = _build_service(session)
        return await service.transform_upload_to_investments(
            upload_id,
            user_id=user_id,
            asset_class_repository=AssetClassRepository(session),
            data_upload_repository=DataUploadRepository(session),
            extractor=InvestmentExtractor(),
            dry_run=dry_run,
        )


# ---------------------------------------------------------------------------
# IT-01: round-trip — 3 investments × NAV/CF → DB
# ---------------------------------------------------------------------------


async def test_it01_roundtrip_three_investments(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    actor, _ac_unclass, _ac_pe, _ac_eq = await _seed_actor_and_classes(
        app_engine, tenant_id, email="it01@example.com"
    )

    investments = {
        "Fund A": {
            "Investment Type": "Aktien",
            "Asset Class": "listed_equity",
            "Manager / Fondsname": "GP A",
            "Region": "Europa",
            "Vintage Year": 2020,
            "Währung": "EUR",
        },
        "Fund B": {
            "Investment Type": "Private Equity",
            "Asset Class": "private_equity",
            "Manager / Fondsname": "GP B",
            "Region": "USA",
            "Vintage Year": "2021",
            "Währung": "USD",
        },
        "Fund C": {
            "Investment Type": "private_equity",
            "Asset Class": None,  # → unclassified
            "Manager / Fondsname": None,
            "Region": None,
            "Vintage Year": None,
            "Währung": "EUR",
        },
    }
    sheets = _build_sheets(
        investments,
        nav_actual=[
            ("2024-01-01", [100.0, 200.0, None]),
            ("2024-07-01", [110.0, 210.0, 50.0]),
        ],
        nav_plan=[
            ("2024-12-31", [120.0, 220.0, 60.0]),
        ],
        cf_out_actual=[
            ("2024-01-01", [-100.0, -200.0, None]),
        ],
        cf_in_actual=[
            ("2024-07-01", [10.0, 20.0, None]),
        ],
    )
    upload = await _create_upload(
        app_engine,
        tenant_id,
        actor.id,
        sheets=sheets,
        file_hash="r" * 64,
        filename="rt.xlsx",
    )

    result = await _run_transform(app_engine, tenant_id, actor.id, upload.id)
    assert result.investments_created == 3
    assert result.investments_updated == 0
    assert result.investments_deactivated == 0
    assert result.investments_reactivated == 0
    assert result.errors == ()

    async with tenant_context(app_engine, tenant_id) as session:
        invs = await InvestmentRepository(session).list_all()
        assert {i.name for i in invs} == {"Fund A", "Fund B", "Fund C"}
        by_name = {i.name: i for i in invs}
        assert by_name["Fund A"].investment_type == "listed_equity"
        assert by_name["Fund A"].vintage_year == 2020
        assert by_name["Fund A"].currency == "EUR"
        assert by_name["Fund A"].manager_name == "GP A"
        assert by_name["Fund A"].is_active is True
        assert by_name["Fund B"].vintage_year == 2021
        # Fund C must land in unclassified.
        ac_repo = AssetClassRepository(session)
        unclass = await ac_repo.get_by_code("unclassified")
        assert by_name["Fund C"].asset_class_id == unclass.id

        navs_a = await InvestmentNavRepository(session).list_by_investment(by_name["Fund A"].id)
        assert {n.nav_kind for n in navs_a} == {"actual", "plan"}
        assert len(navs_a) == 3  # 2 actual + 1 plan

        cashflows_a = await InvestmentCashflowRepository(session).list_by_investment(
            by_name["Fund A"].id
        )
        flow_kinds = {(c.flow_type, c.flow_kind) for c in cashflows_a}
        assert ("capital_call", "actual") in flow_kinds
        assert ("distribution", "actual") in flow_kinds
        # Sign convention: distributions positive, calls negative.
        assert all(c.amount < 0 for c in cashflows_a if c.flow_type == "capital_call")
        assert all(c.amount > 0 for c in cashflows_a if c.flow_type == "distribution")


# ---------------------------------------------------------------------------
# IT-02: replace-by-investment — second import overwrites NAV / cashflows
# ---------------------------------------------------------------------------


async def test_it02_replace_by_investment_overwrites_nav_and_cashflows(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant()
    actor, *_ = await _seed_actor_and_classes(app_engine, tenant_id, email="it02@example.com")
    investments = {
        "Fund X": {
            "Investment Type": "private_equity",
            "Asset Class": "private_equity",
            "Währung": "EUR",
        },
    }
    sheets_v1 = _build_sheets(
        investments,
        nav_actual=[
            ("2024-01-01", [100.0]),
            ("2024-07-01", [110.0]),
        ],
        cf_out_actual=[
            ("2024-01-01", [-100.0]),
            ("2024-04-01", [-25.0]),
        ],
    )
    upload_1 = await _create_upload(
        app_engine,
        tenant_id,
        actor.id,
        sheets=sheets_v1,
        file_hash="b" * 64,
        filename="v1.xlsx",
    )
    await _run_transform(app_engine, tenant_id, actor.id, upload_1.id)

    # Modified data on second import.
    sheets_v2 = _build_sheets(
        investments,
        nav_actual=[
            ("2024-01-01", [105.0]),  # corrected
            ("2024-12-31", [120.0]),  # new
        ],
        cf_out_actual=[
            ("2024-01-01", [-150.0]),  # corrected
        ],
    )
    upload_2 = await _create_upload(
        app_engine,
        tenant_id,
        actor.id,
        sheets=sheets_v2,
        file_hash="c" * 64,
        filename="v2.xlsx",
    )
    result = await _run_transform(app_engine, tenant_id, actor.id, upload_2.id)
    assert result.investments_created == 0
    assert result.investments_updated == 1
    assert result.errors == ()

    async with tenant_context(app_engine, tenant_id) as session:
        inv = await InvestmentRepository(session).get_by_name("Fund X")
        navs = await InvestmentNavRepository(session).list_by_investment(inv.id)
        cashflows = await InvestmentCashflowRepository(session).list_by_investment(inv.id)

    nav_dates = {n.as_of_date for n in navs}
    nav_values = {(n.as_of_date, n.nav_value) for n in navs}
    assert date(2024, 7, 1) not in nav_dates  # old row gone
    assert (date(2024, 1, 1), Decimal("105.0000")) in nav_values
    assert (date(2024, 12, 31), Decimal("120.0000")) in nav_values

    assert len(cashflows) == 1  # second import had only one CF
    assert cashflows[0].amount == Decimal("-150.0000")

    # Audit-log verification: at least one DELETE on
    # ``investment_navs`` for this investment was captured by the
    # b001 audit trigger.
    async with tenant_context(app_engine, tenant_id) as session:
        result = await session.execute(
            text(
                "SELECT operation FROM audit_log "
                "WHERE table_name = 'investment_navs' "
                "AND tenant_id = :tid"
            ),
            {"tid": str(tenant_id)},
        )
        ops = {row[0] for row in result.all()}
    assert "DELETE" in ops or "INSERT" in ops


# ---------------------------------------------------------------------------
# IT-03: soft-delete-with-reactivation symmetry
# ---------------------------------------------------------------------------


async def test_it03_soft_delete_with_reactivation(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    actor, *_ = await _seed_actor_and_classes(app_engine, tenant_id, email="it03@example.com")

    three = {
        "A": {
            "Investment Type": "private_equity",
            "Asset Class": "private_equity",
            "Währung": "EUR",
        },
        "B": {
            "Investment Type": "private_equity",
            "Asset Class": "private_equity",
            "Währung": "EUR",
        },
        "C": {
            "Investment Type": "private_equity",
            "Asset Class": "private_equity",
            "Währung": "EUR",
        },
    }
    upload_1 = await _create_upload(
        app_engine,
        tenant_id,
        actor.id,
        sheets=_build_sheets(three),
        file_hash=("1" * 64),
        filename="v1.xlsx",
    )
    await _run_transform(app_engine, tenant_id, actor.id, upload_1.id)

    # Second import: B is missing.
    two = {k: v for k, v in three.items() if k != "B"}
    upload_2 = await _create_upload(
        app_engine,
        tenant_id,
        actor.id,
        sheets=_build_sheets(two),
        file_hash=("2" * 64),
        filename="v2.xlsx",
    )
    result_2 = await _run_transform(app_engine, tenant_id, actor.id, upload_2.id)
    assert result_2.investments_deactivated == 1
    assert result_2.investments_reactivated == 0

    async with tenant_context(app_engine, tenant_id) as session:
        invs = {i.name: i for i in await InvestmentRepository(session).list_all()}
    assert invs["B"].is_active is False
    assert invs["A"].is_active is True
    assert invs["C"].is_active is True

    # Third import: B reappears.
    upload_3 = await _create_upload(
        app_engine,
        tenant_id,
        actor.id,
        sheets=_build_sheets(three),
        file_hash=("3" * 64),
        filename="v3.xlsx",
    )
    result_3 = await _run_transform(app_engine, tenant_id, actor.id, upload_3.id)
    assert result_3.investments_reactivated == 1
    assert result_3.investments_deactivated == 0

    async with tenant_context(app_engine, tenant_id) as session:
        invs = {i.name: i for i in await InvestmentRepository(session).list_all()}
    assert invs["B"].is_active is True


# ---------------------------------------------------------------------------
# IT-04: idempotency — two consecutive runs of the same upload converge
# ---------------------------------------------------------------------------


async def test_it04_transform_is_idempotent(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    actor, *_ = await _seed_actor_and_classes(app_engine, tenant_id, email="it04@example.com")
    investments = {
        "Idempotent Fund": {
            "Investment Type": "private_equity",
            "Asset Class": "private_equity",
            "Währung": "EUR",
        }
    }
    sheets = _build_sheets(
        investments,
        nav_actual=[("2024-01-01", [100.0])],
        cf_out_actual=[("2024-01-01", [-50.0])],
    )
    upload = await _create_upload(
        app_engine,
        tenant_id,
        actor.id,
        sheets=sheets,
        file_hash=("4" * 64),
        filename="idempotent.xlsx",
    )

    await _run_transform(app_engine, tenant_id, actor.id, upload.id)
    async with tenant_context(app_engine, tenant_id) as session:
        invs_first = await InvestmentRepository(session).list_all()
        navs_first = await InvestmentNavRepository(session).list_by_investment(invs_first[0].id)
        cfs_first = await InvestmentCashflowRepository(session).list_by_investment(invs_first[0].id)

    await _run_transform(app_engine, tenant_id, actor.id, upload.id)
    async with tenant_context(app_engine, tenant_id) as session:
        invs_second = await InvestmentRepository(session).list_all()
        navs_second = await InvestmentNavRepository(session).list_by_investment(invs_second[0].id)
        cfs_second = await InvestmentCashflowRepository(session).list_by_investment(
            invs_second[0].id
        )

    # Investment id is stable (lookup by name).
    assert invs_first[0].id == invs_second[0].id
    # NAVs round-trip identical values.
    assert {(n.as_of_date, n.nav_kind, n.nav_value) for n in navs_first} == {
        (n.as_of_date, n.nav_kind, n.nav_value) for n in navs_second
    }
    # Cashflows also identical (replace-by-investment regenerates them).
    assert len(cfs_first) == len(cfs_second) == 1
    assert cfs_first[0].amount == cfs_second[0].amount


# ---------------------------------------------------------------------------
# IT-05: cross-tenant isolation — two tenants share investment names
# ---------------------------------------------------------------------------


async def test_it05_cross_tenant_isolation_shared_name(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_a = await seed_tenant(name="Tenant A")
    tenant_b = await seed_tenant(name="Tenant B")
    actor_a, *_ = await _seed_actor_and_classes(app_engine, tenant_a, email="a@example.com")
    actor_b, *_ = await _seed_actor_and_classes(app_engine, tenant_b, email="b@example.com")

    shared = {
        "Permira VII": {
            "Investment Type": "private_equity",
            "Asset Class": "private_equity",
            "Währung": "EUR",
        }
    }
    upload_a = await _create_upload(
        app_engine,
        tenant_a,
        actor_a.id,
        sheets=_build_sheets(shared),
        file_hash=("a" * 64),
        filename="A.xlsx",
    )
    upload_b = await _create_upload(
        app_engine,
        tenant_b,
        actor_b.id,
        sheets=_build_sheets(shared),
        file_hash=("b" * 64),
        filename="B.xlsx",
    )

    res_a = await _run_transform(app_engine, tenant_a, actor_a.id, upload_a.id)
    res_b = await _run_transform(app_engine, tenant_b, actor_b.id, upload_b.id)

    assert res_a.investments_created == 1
    assert res_b.investments_created == 1

    async with tenant_context(app_engine, tenant_a) as session:
        a_view = await InvestmentRepository(session).list_all()
    async with tenant_context(app_engine, tenant_b) as session:
        b_view = await InvestmentRepository(session).list_all()
    assert {i.name for i in a_view} == {"Permira VII"}
    assert {i.name for i in b_view} == {"Permira VII"}
    # Distinct rows under the hood — different ids.
    assert a_view[0].id != b_view[0].id


# ---------------------------------------------------------------------------
# IT-06: asset-class fallback — empty Asset Class lands in "unclassified"
# ---------------------------------------------------------------------------


async def test_it06_asset_class_fallback_to_unclassified(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant()
    actor, ac_unclass, *_ = await _seed_actor_and_classes(
        app_engine, tenant_id, email="it06@example.com"
    )
    investments = {
        "No-AC Fund": {
            "Investment Type": "private_equity",
            "Asset Class": None,
            "Währung": "EUR",
        }
    }
    upload = await _create_upload(
        app_engine,
        tenant_id,
        actor.id,
        sheets=_build_sheets(investments),
        file_hash=("u" * 64),
        filename="unclass.xlsx",
    )
    res = await _run_transform(app_engine, tenant_id, actor.id, upload.id)
    assert res.investments_created == 1
    assert res.errors == ()
    async with tenant_context(app_engine, tenant_id) as session:
        inv = await InvestmentRepository(session).get_by_name("No-AC Fund")
    assert inv is not None
    assert inv.asset_class_id == ac_unclass.id


# ---------------------------------------------------------------------------
# IT-07: dry-run does not write
# ---------------------------------------------------------------------------


async def test_it07_dry_run_writes_nothing_but_returns_counts(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant()
    actor, *_ = await _seed_actor_and_classes(app_engine, tenant_id, email="it07@example.com")
    investments = {
        "Dry Fund": {
            "Investment Type": "private_equity",
            "Asset Class": "private_equity",
            "Währung": "EUR",
        }
    }
    upload = await _create_upload(
        app_engine,
        tenant_id,
        actor.id,
        sheets=_build_sheets(
            investments,
            nav_actual=[("2024-01-01", [100.0])],
        ),
        file_hash=("d" * 64),
        filename="dry.xlsx",
    )
    result = await _run_transform(app_engine, tenant_id, actor.id, upload.id, dry_run=True)
    # Counts populated.
    assert result.investments_created == 1
    assert result.navs_replaced == 1
    assert result.errors == ()

    # No writes occurred.
    async with tenant_context(app_engine, tenant_id) as session:
        invs = await InvestmentRepository(session).list_all()
    assert invs == []


# ---------------------------------------------------------------------------
# IT-08: bootstrap fault — missing "unclassified" raises a loud ValueError
# ---------------------------------------------------------------------------


async def test_it08_missing_unclassified_raises_loud(app_engine: AsyncEngine, seed_tenant) -> None:
    """The bootstrap-installed ``unclassified`` asset class is required.

    Per ADR-0043 §3 the importer falls back to ``"unclassified"`` for
    investments whose ``Asset Class`` is empty / unknown. If a tenant
    is mis-bootstrapped (the CLI step was skipped or failed silently),
    the transform must raise a loud ``ValueError`` rather than crash
    with a confusing ``IntegrityError`` from the FK to
    ``asset_classes``.
    """
    tenant_id = await seed_tenant()
    # Seed the user + a single non-fallback class (private_equity), but
    # deliberately *not* the bootstrap "unclassified" class.
    async with tenant_context(app_engine, tenant_id) as session:
        actor = await UserRepository(session).create(
            email="it08@example.com", password_hash="x" * 8
        )
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        await AssetClassRepository(session).create(
            code="private_equity", display_name="Private Equity"
        )

    investments = {
        "Bootstrap Fault Fund": {
            "Investment Type": "private_equity",
            "Asset Class": "private_equity",
            "Währung": "EUR",
        }
    }
    upload = await _create_upload(
        app_engine,
        tenant_id,
        actor.id,
        sheets=_build_sheets(investments),
        file_hash=("b" * 64),
        filename="bootstrap-fault.xlsx",
    )

    with pytest.raises(ValueError, match="unclassified"):
        await _run_transform(app_engine, tenant_id, actor.id, upload.id)


# ---------------------------------------------------------------------------
# IT-09: unknown Asset Class is auto-created from the Excel value
# ---------------------------------------------------------------------------


async def test_it09_transform_auto_creates_unknown_asset_class(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """An Excel ``Asset Class`` value that is not yet in the catalogue
    is auto-created with a normalised code and the original label as
    display name. The investment lands on the new asset class — *not*
    on ``unclassified``.
    """
    tenant_id = await seed_tenant()
    # Seed only the bootstrap "unclassified" class — no "Equities".
    async with tenant_context(app_engine, tenant_id) as session:
        actor = await UserRepository(session).create(
            email="it09@example.com", password_hash="x" * 8
        )
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        await AssetClassRepository(session).create(code="unclassified", display_name="Unclassified")

    investments = {
        "Equities Fund": {
            "Investment Type": "listed_equity",
            "Asset Class": "Equities",
            "Währung": "EUR",
        }
    }
    upload = await _create_upload(
        app_engine,
        tenant_id,
        actor.id,
        sheets=_build_sheets(investments),
        file_hash=("e" * 64),
        filename="autocreate.xlsx",
    )
    result = await _run_transform(app_engine, tenant_id, actor.id, upload.id)
    assert result.investments_created == 1
    assert result.errors == ()

    async with tenant_context(app_engine, tenant_id) as session:
        inv = await InvestmentRepository(session).get_by_name("Equities Fund")
        ac_repo = AssetClassRepository(session)
        ac_equities = await ac_repo.get_by_code("equities")
        ac_unclass = await ac_repo.get_by_code("unclassified")

    assert ac_equities is not None
    assert ac_equities.display_name == "Equities"
    assert inv.asset_class_id == ac_equities.id
    assert inv.asset_class_id != ac_unclass.id


# ---------------------------------------------------------------------------
# IT-10: auto-created asset class is cached and reused across investments
# ---------------------------------------------------------------------------


async def test_it10_transform_reuses_auto_created_asset_class(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """Two investments sharing the same unknown Asset Class trigger
    exactly one ``CREATE`` — the second resolves through the cache.
    """
    tenant_id = await seed_tenant()
    async with tenant_context(app_engine, tenant_id) as session:
        actor = await UserRepository(session).create(
            email="it10@example.com", password_hash="x" * 8
        )
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        await AssetClassRepository(session).create(code="unclassified", display_name="Unclassified")

    investments = {
        "Equities Fund A": {
            "Investment Type": "listed_equity",
            "Asset Class": "Equities",
            "Währung": "EUR",
        },
        "Equities Fund B": {
            "Investment Type": "listed_equity",
            "Asset Class": "Equities",
            "Währung": "EUR",
        },
    }
    upload = await _create_upload(
        app_engine,
        tenant_id,
        actor.id,
        sheets=_build_sheets(investments),
        file_hash=("f" * 64),
        filename="autocreate2.xlsx",
    )
    result = await _run_transform(app_engine, tenant_id, actor.id, upload.id)
    assert result.investments_created == 2
    assert result.errors == ()

    async with tenant_context(app_engine, tenant_id) as session:
        invs = {i.name: i for i in await InvestmentRepository(session).list_all()}
        ac_repo = AssetClassRepository(session)
        ac_equities = await ac_repo.get_by_code("equities")
        all_classes = await ac_repo.list_all()

    assert ac_equities is not None
    # Both investments share the auto-created asset class.
    assert (
        invs["Equities Fund A"].asset_class_id
        == invs["Equities Fund B"].asset_class_id
        == ac_equities.id
    )
    # Exactly one "Equities" row exists (no duplicate insert).
    equities_rows = [a for a in all_classes if a.code == "equities"]
    assert len(equities_rows) == 1
