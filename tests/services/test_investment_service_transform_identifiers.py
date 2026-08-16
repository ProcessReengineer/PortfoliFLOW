# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""End-to-end tests for ``transform_upload_to_investments`` identifiers.

Exercises the opt-in security-identifier reconciliation path (ADR-0090
§"Identifiers enter through both import paths"). Each test seeds a
tenant + user + asset-class catalogue + a ``data_uploads`` row whose
Attributes sheet carries ``ISIN`` / ``Ticker`` rows, then runs the
transform with the identifier repository wired up.

Primary promotion is **type-aware** (ADR-0091): a market-linked type
(``listed_equity`` / ``listed_bonds``) promotes its **ticker** first — the
only wired live adapter (Yahoo) routes ``ticker`` only — while every other
type promotes its **ISIN** first. A manual primary of any source is never
overridden.

Coverage
--------
* ID-01 first import: identifiers land with ``source='excel'``; a
  market-linked type promotes its **ticker** to primary; a ticker-only
  investment gets ticker as primary.
* ID-02 idempotent re-import: the same workbook twice → identical row
  set, no constraint violation.
* ID-03 reconciliation: an identifier dropped from the workbook
  disappears (``source='excel'`` only); a pre-existing ``manual`` row
  survives; the fallback primary is re-promoted once its predecessor is
  dropped.
* ID-04 an identifier that migrates between investments across two
  uploads succeeds — deletions run before insertions.
* ID-05 a non-market-linked type (``private_equity``) keeps **ISIN**-first
  promotion.
* ID-06 an existing ``manual`` primary is not re-promoted on re-import.
"""

from __future__ import annotations

from uuid import UUID

import pandas as pd
from sqlalchemy.ext.asyncio import AsyncEngine

from core.repositories import (
    AssetClassRepository,
    DataUploadRepository,
    InvestmentCashflowRepository,
    InvestmentIdentifierRepository,
    InvestmentNavRepository,
    InvestmentRepository,
    UserRepository,
    tenant_context,
)
from services.data_normalization import InvestmentExtractor
from services.investments import InvestmentService


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _attributes_df(investments: dict[str, dict[str, object]]) -> pd.DataFrame:
    """Build an Attributes DataFrame with scalar attrs + ISIN / Ticker rows.

    Each investment dict may carry ``"ISIN"`` / ``"Ticker"`` keys; a
    missing key leaves that cell blank (the illiquid-instrument state).
    Both identifier rows are always emitted so an omitted key models
    "identifier removed from this workbook version".
    """
    columns = list(investments.keys())
    rows = [
        "Investment Type",
        "Asset Class",
        "Währung",
        "ISIN",
        "Ticker",
    ]
    data: list[list[object]] = []
    index: list[str] = []
    for label in rows:
        index.append(label)
        data.append([investments[col].get(label) for col in columns])
    return pd.DataFrame(data, index=index, columns=columns)


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
    file_hash: str,
):
    sheets = {"attributes": _attributes_df(investments)}
    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        return await DataUploadRepository(session).create_upload(
            uploaded_by=user_id,
            filename="ids.xlsx",
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
):
    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        service = InvestmentService(
            investments=InvestmentRepository(session),
            navs=InvestmentNavRepository(session),
            cashflows=InvestmentCashflowRepository(session),
        )
        return await service.transform_upload_to_investments(
            upload_id,
            user_id=user_id,
            asset_class_repository=AssetClassRepository(session),
            data_upload_repository=DataUploadRepository(session),
            extractor=InvestmentExtractor(),
            investment_identifier_repository=(InvestmentIdentifierRepository(session)),
        )


def _rowset(rows) -> set[tuple[str, str, bool, str | None]]:
    return {(r.scheme, r.value, r.is_primary, r.source) for r in rows}


def _liquid(**extra: object) -> dict[str, object]:
    base: dict[str, object] = {
        "Investment Type": "listed_equity",
        "Asset Class": "listed_equity",
        "Währung": "EUR",
    }
    base.update(extra)
    return base


def _private(**extra: object) -> dict[str, object]:
    """A non-market-linked (private_equity) investment attribute dict."""
    base: dict[str, object] = {
        "Investment Type": "private_equity",
        "Asset Class": "private_equity",
        "Währung": "EUR",
    }
    base.update(extra)
    return base


# ---------------------------------------------------------------------------
# ID-01: first import persists + promotes primary
# ---------------------------------------------------------------------------


async def test_id01_first_import_persists_and_promotes_primary(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant()
    actor = await _seed(app_engine, tenant_id, email="id01@example.com")

    investments = {
        # Lower-case input proves the repository upper-cases on write.
        "Fund Isin": _liquid(ISIN="us0378331005", Ticker="AAPL"),
        "Fund Ticker": _liquid(Ticker="msft"),
    }
    upload = await _create_upload(
        app_engine,
        tenant_id,
        actor.id,
        investments=investments,
        file_hash="id01",
    )
    result = await _run_transform(app_engine, tenant_id, actor.id, upload.id)
    assert result.investments_created == 2
    assert result.errors == ()

    async with tenant_context(app_engine, tenant_id) as session:
        inv_repo = InvestmentRepository(session)
        id_repo = InvestmentIdentifierRepository(session)
        isin_inv = await inv_repo.get_by_name("Fund Isin")
        ticker_inv = await inv_repo.get_by_name("Fund Ticker")
        isin_rows = await id_repo.list_for_investment(isin_inv.id)
        ticker_rows = await id_repo.list_for_investment(ticker_inv.id)

    # listed_equity is market-linked (Yahoo routes ticker only), so the
    # TICKER is promoted to primary; both rows carry source='excel'.
    assert _rowset(isin_rows) == {
        ("isin", "US0378331005", False, "excel"),
        ("ticker", "AAPL", True, "excel"),
    }
    # Ticker-only investment: the ticker becomes primary (unchanged).
    assert _rowset(ticker_rows) == {
        ("ticker", "MSFT", True, "excel"),
    }


# ---------------------------------------------------------------------------
# ID-02: idempotent re-import
# ---------------------------------------------------------------------------


async def test_id02_reimport_is_idempotent(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    actor = await _seed(app_engine, tenant_id, email="id02@example.com")

    investments = {"Fund A": _liquid(ISIN="US0378331005", Ticker="AAPL")}

    upload_1 = await _create_upload(
        app_engine,
        tenant_id,
        actor.id,
        investments=investments,
        file_hash="id02-1",
    )
    await _run_transform(app_engine, tenant_id, actor.id, upload_1.id)

    async with tenant_context(app_engine, tenant_id) as session:
        inv = await InvestmentRepository(session).get_by_name("Fund A")
        snap_1 = _rowset(await InvestmentIdentifierRepository(session).list_for_investment(inv.id))

    upload_2 = await _create_upload(
        app_engine,
        tenant_id,
        actor.id,
        investments=investments,
        file_hash="id02-2",
    )
    result_2 = await _run_transform(app_engine, tenant_id, actor.id, upload_2.id)
    assert result_2.errors == ()

    async with tenant_context(app_engine, tenant_id) as session:
        inv = await InvestmentRepository(session).get_by_name("Fund A")
        snap_2 = _rowset(await InvestmentIdentifierRepository(session).list_for_investment(inv.id))

    # listed_equity → ticker-first promotion; stable across the re-import.
    assert (
        snap_2
        == snap_1
        == {
            ("isin", "US0378331005", False, "excel"),
            ("ticker", "AAPL", True, "excel"),
        }
    )


# ---------------------------------------------------------------------------
# ID-03: reconciliation — excel-subset delete, manual survives, primary kept
# ---------------------------------------------------------------------------


async def test_id03_reconciliation_excel_subset_and_manual_survives(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant()
    actor = await _seed(app_engine, tenant_id, email="id03@example.com")

    # Import 1: ISIN + Ticker, both from Excel; the ticker becomes primary
    # (listed_equity is market-linked → ticker-first).
    upload_1 = await _create_upload(
        app_engine,
        tenant_id,
        actor.id,
        investments={"Fund Recon": _liquid(ISIN="US0378331005", Ticker="AAPL")},
        file_hash="id03-1",
    )
    await _run_transform(app_engine, tenant_id, actor.id, upload_1.id)

    # A manual identifier is added out-of-band between imports.
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        inv = await InvestmentRepository(session).get_by_name("Fund Recon")
        await InvestmentIdentifierRepository(session).add(
            investment_id=inv.id,
            scheme="internal",
            value="LEGACY-1",
            created_by=actor.id,
            source="manual",
        )

    # Import 2: the Ticker row is dropped from the workbook (ISIN only).
    upload_2 = await _create_upload(
        app_engine,
        tenant_id,
        actor.id,
        investments={"Fund Recon": _liquid(ISIN="US0378331005")},
        file_hash="id03-2",
    )
    result = await _run_transform(app_engine, tenant_id, actor.id, upload_2.id)
    assert result.errors == ()

    async with tenant_context(app_engine, tenant_id) as session:
        inv = await InvestmentRepository(session).get_by_name("Fund Recon")
        rows = await InvestmentIdentifierRepository(session).list_for_investment(inv.id)

    assert _rowset(rows) == {
        # Import 2 drops the ticker (the import-1 primary), so Phase 4
        # re-promotes the only market-usable row left — the ISIN.
        ("isin", "US0378331005", True, "excel"),
        # The out-of-band manual row is untouched.
        ("internal", "LEGACY-1", False, "manual"),
    }
    # The Excel ticker, dropped from the workbook, is gone.
    assert all(r.scheme != "ticker" for r in rows)


# ---------------------------------------------------------------------------
# ID-04: an identifier moves investments between uploads (delete before insert)
# ---------------------------------------------------------------------------


async def test_id04_identifier_moves_between_investments(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant()
    actor = await _seed(app_engine, tenant_id, email="id04@example.com")

    # Upload 1: ISIN on Fund X; Fund Y has none.
    upload_1 = await _create_upload(
        app_engine,
        tenant_id,
        actor.id,
        investments={
            "Fund X": _liquid(ISIN="US0378331005"),
            "Fund Y": _liquid(),
        },
        file_hash="id04-1",
    )
    await _run_transform(app_engine, tenant_id, actor.id, upload_1.id)

    # Upload 2: the same ISIN now belongs to Fund Y (removed from X). If
    # insertions ran before deletions this would trip the partial
    # per-tenant unique index; delete-before-insert makes it succeed.
    upload_2 = await _create_upload(
        app_engine,
        tenant_id,
        actor.id,
        investments={
            "Fund X": _liquid(),
            "Fund Y": _liquid(ISIN="US0378331005"),
        },
        file_hash="id04-2",
    )
    result = await _run_transform(app_engine, tenant_id, actor.id, upload_2.id)
    assert result.errors == ()

    async with tenant_context(app_engine, tenant_id) as session:
        inv_repo = InvestmentRepository(session)
        id_repo = InvestmentIdentifierRepository(session)
        x = await inv_repo.get_by_name("Fund X")
        y = await inv_repo.get_by_name("Fund Y")
        x_rows = await id_repo.list_for_investment(x.id)
        y_rows = await id_repo.list_for_investment(y.id)

    assert x_rows == []
    assert _rowset(y_rows) == {
        ("isin", "US0378331005", True, "excel"),
    }


# ---------------------------------------------------------------------------
# ID-05: a non-market-linked type keeps ISIN-first promotion
# ---------------------------------------------------------------------------


async def test_id05_non_market_linked_keeps_isin_first(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant()
    actor = await _seed(app_engine, tenant_id, email="id05@example.com")

    # A private-markets type carrying both an ISIN and a ticker. It is NOT
    # market-linked, so the historical ISIN-first preference stands — the
    # ticker-first rule is scoped to MARKET_LINKED_TYPES only.
    upload = await _create_upload(
        app_engine,
        tenant_id,
        actor.id,
        investments={"PE Fund": _private(ISIN="US0378331005", Ticker="AAPL")},
        file_hash="id05",
    )
    result = await _run_transform(app_engine, tenant_id, actor.id, upload.id)
    assert result.errors == ()

    async with tenant_context(app_engine, tenant_id) as session:
        inv = await InvestmentRepository(session).get_by_name("PE Fund")
        rows = await InvestmentIdentifierRepository(session).list_for_investment(inv.id)

    # ISIN promoted to primary (non-market-linked → ISIN-first).
    assert _rowset(rows) == {
        ("isin", "US0378331005", True, "excel"),
        ("ticker", "AAPL", False, "excel"),
    }


# ---------------------------------------------------------------------------
# ID-06: an existing manual primary is not re-promoted on re-import
# ---------------------------------------------------------------------------


async def test_id06_existing_manual_primary_is_not_re_promoted(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant()
    actor = await _seed(app_engine, tenant_id, email="id06@example.com")

    # First import: market-linked → the ticker is promoted to primary.
    upload_1 = await _create_upload(
        app_engine,
        tenant_id,
        actor.id,
        investments={"Fund M": _liquid(ISIN="US0378331005", Ticker="AAPL")},
        file_hash="id06-1",
    )
    await _run_transform(app_engine, tenant_id, actor.id, upload_1.id)

    # The operator adds a manual identifier and makes it the primary by hand
    # (demote-then-promote, the ADR-0096 CRUD idiom) — the deliberate choice
    # the no-override invariant must protect across re-imports.
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        id_repo = InvestmentIdentifierRepository(session)
        inv = await InvestmentRepository(session).get_by_name("Fund M")
        rows = await id_repo.list_for_investment(inv.id)
        ticker_row = next(r for r in rows if r.scheme == "ticker")
        await id_repo.set_primary(ticker_row.id, is_primary=False)
        await id_repo.add(
            investment_id=inv.id,
            scheme="figi",
            value="BBG000B9XRY4",
            created_by=actor.id,
            source="manual",
            is_primary=True,
        )

    # Re-import the same workbook: the ticker-first rule must NOT re-assert
    # itself — promotion fires only when there is no primary at all.
    upload_2 = await _create_upload(
        app_engine,
        tenant_id,
        actor.id,
        investments={"Fund M": _liquid(ISIN="US0378331005", Ticker="AAPL")},
        file_hash="id06-2",
    )
    result = await _run_transform(app_engine, tenant_id, actor.id, upload_2.id)
    assert result.errors == ()

    async with tenant_context(app_engine, tenant_id) as session:
        inv = await InvestmentRepository(session).get_by_name("Fund M")
        rows = await InvestmentIdentifierRepository(session).list_for_investment(inv.id)

    # The hand-picked manual FIGI primary is intact; ticker/isin stay
    # non-primary excel rows.
    assert _rowset(rows) == {
        ("figi", "BBG000B9XRY4", True, "manual"),
        ("isin", "US0378331005", False, "excel"),
        ("ticker", "AAPL", False, "excel"),
    }
