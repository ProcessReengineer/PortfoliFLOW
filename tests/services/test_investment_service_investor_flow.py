# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Service tests for the investor-flow cash-only booking rule (ADR-0103 §5).

ADR-0103 §5: an investor flow — a net contribution to, or withdrawal from,
the mandate — is booked on the **cash position of the currency it settles
in** (decision N4), and "a validation rule rejects ``investor_flow`` rows on
non-cash investments".

The rule spans two tables (``investment_cashflows.flow_type`` and
``investments.investment_type``), which no DB CHECK can see across, so
:class:`services.investments.InvestmentService` owns it and every caller —
the web CRUD surface today, anything later — inherits it from there. These
tests exercise the seam directly.

Coverage

* ``add_cashflow`` accepts ``investor_flow`` on a cash investment across the
  whole legal space: both ``flow_kind`` variants × both amount signs. No
  sign constraint exists for this flow type — a contribution and a
  withdrawal are the same type with opposite signs.
* ``add_cashflow`` rejects it on a non-cash investment with the typed
  :class:`core.exceptions.InvestorFlowScopeError`, and writes nothing.
* ``update_cashflow`` rejects a flow-type change **to** ``investor_flow`` on
  a non-cash investment (the row's own type is what the rule reads, so an
  update that merely retains it is checked too).
* ``update_cashflow`` permits changing a cash row's flow type **away** from
  ``investor_flow`` — that direction is unrestricted.
* The other seven flow types are entirely unaffected on a cash investment
  (``'other'`` rows remain legal there, ADR-0103 §8).
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from core.exceptions import InvestorFlowScopeError
from core.repositories import (
    AssetClassRepository,
    InvestmentCashflowRepository,
    InvestmentNavRepository,
    InvestmentRepository,
    UserRepository,
    tenant_context,
)
from services.investments import InvestmentService

_TS = datetime(2025, 6, 15, 12, 0, tzinfo=timezone.utc)


def _build_service(session) -> InvestmentService:
    return InvestmentService(
        investments=InvestmentRepository(session),
        navs=InvestmentNavRepository(session),
        cashflows=InvestmentCashflowRepository(session),
    )


async def _seed_actor_and_asset_class(app_engine: AsyncEngine, tenant_id, *, email: str):
    async with tenant_context(app_engine, tenant_id) as session:
        actor = await UserRepository(session).create(email=email, password_hash="x" * 8)
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        asset_class = await AssetClassRepository(session).create(
            code="default_class", display_name="Default Class"
        )
    return actor, asset_class


# ---------------------------------------------------------------------------
# add_cashflow — the happy path on a cash position
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("flow_kind", "amount"),
    [
        ("actual", Decimal("250000")),  # realised contribution
        ("actual", Decimal("-80000")),  # realised withdrawal
        ("plan", Decimal("500000")),  # planned contribution
        ("plan", Decimal("-120000")),  # planned withdrawal
    ],
)
async def test_investor_flow_accepted_on_cash_both_kinds_both_signs(
    app_engine: AsyncEngine, seed_tenant, flow_kind: str, amount: Decimal
) -> None:
    """Both flow kinds and both signs are legal on a cash position."""
    tenant_id = await seed_tenant()
    actor, ac = await _seed_actor_and_asset_class(
        app_engine, tenant_id, email=f"if-ok-{flow_kind}-{amount}@example.com"
    )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        svc = _build_service(session)
        cash = await svc.create_investment(
            name="Cash EUR",
            investment_type="cash",
            asset_class_id=ac.id,
            currency="EUR",
            created_by=actor.id,
        )
        flow = await svc.add_cashflow(
            investment_id=cash.id,
            flow_timestamp=_TS,
            flow_type="investor_flow",
            flow_kind=flow_kind,
            amount=amount,
            currency="EUR",
            description="Mandate flow",
            created_by=actor.id,
        )

    assert flow.flow_type == "investor_flow"
    assert flow.flow_kind == flow_kind
    assert flow.amount == amount
    # A manual CRUD write is stamped 'manual' (ADR-0092) — unchanged.
    assert flow.ingest_origin == "manual"


async def test_other_flow_types_still_legal_on_a_cash_position(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """The rule constrains ``investor_flow``, not cash (ADR-0103 §8)."""
    tenant_id = await seed_tenant()
    actor, ac = await _seed_actor_and_asset_class(
        app_engine, tenant_id, email="if-other@example.com"
    )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        svc = _build_service(session)
        cash = await svc.create_investment(
            name="Cash EUR",
            investment_type="cash",
            asset_class_id=ac.id,
            currency="EUR",
            created_by=actor.id,
        )
        flow = await svc.add_cashflow(
            investment_id=cash.id,
            flow_timestamp=_TS,
            flow_type="other",
            flow_kind="actual",
            amount=Decimal("-42"),
            currency="EUR",
            description="A bank charge",
            created_by=actor.id,
        )
    assert flow.flow_type == "other"


# ---------------------------------------------------------------------------
# add_cashflow — the rejection on a non-cash position
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "investment_type",
    ["private_equity", "listed_bonds", "real_estate", "other"],
)
async def test_investor_flow_rejected_on_non_cash_investment(
    app_engine: AsyncEngine, seed_tenant, investment_type: str
) -> None:
    """The typed error fires, and nothing is written."""
    tenant_id = await seed_tenant()
    actor, ac = await _seed_actor_and_asset_class(
        app_engine, tenant_id, email=f"if-bad-{investment_type}@example.com"
    )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        svc = _build_service(session)
        fund = await svc.create_investment(
            name="A Fund",
            investment_type=investment_type,
            asset_class_id=ac.id,
            currency="EUR",
            created_by=actor.id,
        )
        with pytest.raises(InvestorFlowScopeError) as excinfo:
            await svc.add_cashflow(
                investment_id=fund.id,
                flow_timestamp=_TS,
                flow_type="investor_flow",
                flow_kind="actual",
                amount=Decimal("1000"),
                currency="EUR",
                description=None,
                created_by=actor.id,
            )
        assert excinfo.value.field == "flow_type"
        assert "cash position" in excinfo.value.message

    # The rejected write left no row behind.
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        detail = await _build_service(session).get_investment_detail(fund.id)
        assert detail is not None
        assert detail.cashflows == []


# ---------------------------------------------------------------------------
# update_cashflow — the same rule on the effective flow type
# ---------------------------------------------------------------------------


async def test_update_to_investor_flow_rejected_on_non_cash_investment(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """Re-typing an existing fund cashflow to ``investor_flow`` is refused."""
    tenant_id = await seed_tenant()
    actor, ac = await _seed_actor_and_asset_class(
        app_engine, tenant_id, email="if-upd-bad@example.com"
    )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        svc = _build_service(session)
        fund = await svc.create_investment(
            name="PE Fund",
            investment_type="private_equity",
            asset_class_id=ac.id,
            currency="EUR",
            created_by=actor.id,
        )
        call = await svc.add_cashflow(
            investment_id=fund.id,
            flow_timestamp=_TS,
            flow_type="capital_call",
            flow_kind="actual",
            amount=Decimal("-1000"),
            currency="EUR",
            description=None,
            created_by=actor.id,
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        svc = _build_service(session)
        with pytest.raises(InvestorFlowScopeError):
            await svc.update_cashflow(call.id, acting_user=actor.id, flow_type="investor_flow")

        # The row is untouched — the guard runs before the write.
        unchanged = await svc.get_cashflow(call.id)
        assert unchanged is not None
        assert unchanged.flow_type == "capital_call"


async def test_update_away_from_investor_flow_is_unrestricted(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """A cash row may be re-typed away from ``investor_flow`` freely."""
    tenant_id = await seed_tenant()
    actor, ac = await _seed_actor_and_asset_class(
        app_engine, tenant_id, email="if-upd-away@example.com"
    )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        svc = _build_service(session)
        cash = await svc.create_investment(
            name="Cash EUR",
            investment_type="cash",
            asset_class_id=ac.id,
            currency="EUR",
            created_by=actor.id,
        )
        flow = await svc.add_cashflow(
            investment_id=cash.id,
            flow_timestamp=_TS,
            flow_type="investor_flow",
            flow_kind="plan",
            amount=Decimal("100000"),
            currency="EUR",
            description=None,
            created_by=actor.id,
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        svc = _build_service(session)
        retyped = await svc.update_cashflow(flow.id, acting_user=actor.id, flow_type="other")
        assert retyped is not None
        assert retyped.flow_type == "other"


async def test_update_retaining_investor_flow_on_cash_still_works(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """An amount-only edit of a cash investor flow passes the guard."""
    tenant_id = await seed_tenant()
    actor, ac = await _seed_actor_and_asset_class(
        app_engine, tenant_id, email="if-upd-retain@example.com"
    )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        svc = _build_service(session)
        cash = await svc.create_investment(
            name="Cash EUR",
            investment_type="cash",
            asset_class_id=ac.id,
            currency="EUR",
            created_by=actor.id,
        )
        flow = await svc.add_cashflow(
            investment_id=cash.id,
            flow_timestamp=_TS,
            flow_type="investor_flow",
            flow_kind="plan",
            amount=Decimal("100000"),
            currency="EUR",
            description=None,
            created_by=actor.id,
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        svc = _build_service(session)
        updated = await svc.update_cashflow(flow.id, acting_user=actor.id, amount=Decimal("150000"))
        assert updated is not None
        assert updated.flow_type == "investor_flow"
        assert updated.amount == Decimal("150000.0000")


async def test_update_of_a_missing_cashflow_still_returns_none(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """Regression: the new pre-read must not turn a miss into an exception."""
    from uuid import uuid4

    tenant_id = await seed_tenant()
    actor, _ = await _seed_actor_and_asset_class(
        app_engine, tenant_id, email="if-upd-missing@example.com"
    )
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        svc = _build_service(session)
        assert await svc.update_cashflow(uuid4(), acting_user=actor.id, amount=Decimal("1")) is None
