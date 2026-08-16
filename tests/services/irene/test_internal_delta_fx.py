# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Propagation of the ADR-0099 §4 conversion boundary into Irene.

:func:`services.irene.internal_delta.evaluate_internal_deltas` is a
consumer of Seam B — it reads the :class:`LimitsCoverageService` bundle
and diffs it against ``irene_watch_state``. It has no currency logic of
its own, so once Seam B converts the coverage NAVs into the functional
currency, Irene's findings follow **with no code change** in the delta
layer. This test proves that end-to-end through the public surface: a
USD position's converted coverage — not its nominal one — drives the
eligible finding.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncEngine

from core.repositories import (
    AssetClassRepository,
    FxRateRepository,
    InvestmentNavRepository,
    InvestmentRepository,
    LimitsRepository,
    tenant_context,
)
from services.irene.internal_delta import evaluate_internal_deltas
from tests.services.irene._book_fixtures import (
    ANCHOR_DATE,
    AUM_EUR,
    LATEST_DATE,
    SAA_SUBJECT,
    D,
    resolution,
    seed_user,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _seed_usd_book(app_engine: AsyncEngine, tenant_id: UUID, user_id: UUID) -> None:
    """A one-USD-investment book breaching only after FX conversion.

    Latest NAV 800k USD × 0.80 = 640k EUR against 1M AUM → 64% coverage,
    over the 50% SAA ceiling → BREACH. The nominal 800k would read 80% —
    the finding must carry the converted 64%.
    """
    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        ac = await AssetClassRepository(session).create(code="equities", display_name="Equities")
        inv = await InvestmentRepository(session).create(
            name="Dollar Fund",
            investment_type="listed_equity",
            asset_class_id=ac.id,
            currency="USD",
            created_by=user_id,
        )

        nav_repo = InvestmentNavRepository(session)
        for as_of, value in (
            (ANCHOR_DATE, D("100000")),
            (LATEST_DATE, D("800000")),
        ):
            await nav_repo.upsert(
                investment_id=inv.id,
                as_of_date=as_of,
                nav_kind="actual",
                nav_value=value,
                currency="USD",
                source=None,
                created_by=user_id,
            )

        await FxRateRepository(session).upsert(
            currency="USD",
            as_of_date=date(2023, 1, 1),  # before both NAV dates
            rate_to_reference=D("0.80"),
            reference_currency="EUR",
            source="excel",
            created_by=user_id,
        )

        # The rest of the book, held as EUR cash rather than asserted as an
        # AUM row (ADR-0103 §2). Sized against the *converted* fund NAV so the
        # book totals AUM_EUR at both dates and the coverage percentages below
        # are the same ones the retired portfolio_aum row produced.
        cash_ac = await AssetClassRepository(session).create(code="cash", display_name="Cash")
        cash = await InvestmentRepository(session).create(
            name="Cash EUR",
            investment_type="cash",
            asset_class_id=cash_ac.id,
            currency="EUR",
            created_by=user_id,
        )
        for as_of, converted in (
            (ANCHOR_DATE, D("80000")),  # 100,000 USD × 0.80
            (LATEST_DATE, D("640000")),  # 800,000 USD × 0.80
        ):
            await nav_repo.upsert(
                investment_id=cash.id,
                as_of_date=as_of,
                nav_kind="actual",
                nav_value=AUM_EUR - converted,
                currency="EUR",
                source=None,
                created_by=user_id,
            )

        limits_repo = LimitsRepository(session)
        await limits_repo.create_set_with_limits(
            family="saa",
            effective_from=date(2020, 1, 1),
            label="SAA test",
            notes=None,
            limits={"equities": D("50.0")},
            created_by=user_id,
        )
        await limits_repo.create_set_with_limits(
            family="anlv",
            effective_from=date(2020, 1, 1),
            label="AnlV test",
            notes=None,
            limits={"anlv_1": D("50.0")},
            created_by=user_id,
        )


async def test_internal_delta_reflects_converted_coverage(
    app_engine: AsyncEngine,
    superuser_engine: AsyncEngine,
    seed_tenant,
) -> None:
    tenant_id = await seed_tenant("irene-fx")
    user_id = await seed_user(superuser_engine, tenant_id)
    await _seed_usd_book(app_engine, tenant_id, user_id)

    async with tenant_context(app_engine, tenant_id) as session:
        eligible = await evaluate_internal_deltas(
            session, now=_now(), resolution=await resolution(session)
        )

    assert len(eligible) == 1
    finding = eligible[0]
    assert finding.subject_key == SAA_SUBJECT
    assert finding.kind == "rising_edge"
    assert finding.status == "BREACH"
    # The converted coverage (64%), NOT the nominal 80% — Irene consumed
    # the already-converted Seam-B bundle without any code change.
    assert finding.coverage_pct == D("64")
