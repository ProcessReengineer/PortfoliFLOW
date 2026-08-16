# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""InvestmentService end-to-end tests against the live compose Postgres.

The service is the right place to test the cross-repository
read/write contract that route handlers (sub-stream 4b) and the
Excel-import workflow (sub-stream 4c) will consume.

Coverage:

* ``list_investments`` and ``list_active_investments`` honour the
  ``is_active`` filter.
* ``get_investment_detail`` aggregates investment + NAVs +
  cashflows.
* ``get_investment_detail`` returns ``None`` for a missing id.
* Lifecycle: create → update → delete.
* ``set_investment_active`` toggles soft-delete.
* ``add_nav`` UPSERTs on ``(investment, date, kind)``.
* ``update_nav`` and ``delete_nav`` operate by NAV id.
* ``add_cashflow`` appends; ``update_cashflow`` and
  ``delete_cashflow`` operate by cashflow id.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncEngine

from core.repositories import (
    AssetClassRepository,
    InvestmentCashflowRepository,
    InvestmentNavRepository,
    InvestmentRepository,
    UserRepository,
    tenant_context,
)
from services.investments import InvestmentService


def _build_service(session) -> InvestmentService:
    """Construct an InvestmentService bound to the given session."""
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
# IS-01: list and list_active honour is_active
# ---------------------------------------------------------------------------


async def test_is01_list_and_list_active(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    actor, ac = await _seed_actor_and_asset_class(app_engine, tenant_id, email="is01@example.com")

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        svc = _build_service(session)
        await svc.create_investment(
            name="Active Fund",
            investment_type="private_equity",
            asset_class_id=ac.id,
            currency="EUR",
            created_by=actor.id,
        )
        inactive = await svc.create_investment(
            name="Inactive Fund",
            investment_type="private_equity",
            asset_class_id=ac.id,
            currency="EUR",
            created_by=actor.id,
        )
        await svc.set_investment_active(inactive.id, False)

    async with tenant_context(app_engine, tenant_id) as session:
        svc = _build_service(session)
        all_inv = await svc.list_investments()
        active_only = await svc.list_active_investments()

    assert {i.name for i in all_inv} == {"Active Fund", "Inactive Fund"}
    assert {i.name for i in active_only} == {"Active Fund"}


# ---------------------------------------------------------------------------
# IS-02: get_investment_detail aggregates investment + navs + cashflows
# ---------------------------------------------------------------------------


async def test_is02_get_investment_detail_aggregates(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    actor, ac = await _seed_actor_and_asset_class(app_engine, tenant_id, email="is02@example.com")

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        svc = _build_service(session)
        investment = await svc.create_investment(
            name="Detail Fund",
            investment_type="private_equity",
            asset_class_id=ac.id,
            currency="EUR",
            created_by=actor.id,
        )
        await svc.add_nav(
            investment_id=investment.id,
            as_of_date=date(2025, 12, 31),
            nav_kind="actual",
            nav_value=Decimal("1000"),
            currency="EUR",
            source=None,
            created_by=actor.id,
        )
        await svc.add_nav(
            investment_id=investment.id,
            as_of_date=date(2026, 6, 30),
            nav_kind="plan",
            nav_value=Decimal("1100"),
            currency="EUR",
            source=None,
            created_by=actor.id,
        )
        await svc.add_cashflow(
            investment_id=investment.id,
            flow_timestamp=datetime(2025, 6, 15, 12, 0, tzinfo=timezone.utc),
            flow_type="capital_call",
            flow_kind="actual",
            amount=Decimal("-500"),
            currency="EUR",
            description=None,
            created_by=actor.id,
        )

    async with tenant_context(app_engine, tenant_id) as session:
        detail = await _build_service(session).get_investment_detail(investment.id)

    assert detail is not None
    assert detail.investment.id == investment.id
    assert len(detail.navs) == 2
    assert len(detail.cashflows) == 1


# ---------------------------------------------------------------------------
# IS-03: get_investment_detail returns None for missing id
# ---------------------------------------------------------------------------


async def test_is03_get_investment_detail_missing_returns_none(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant()
    async with tenant_context(app_engine, tenant_id) as session:
        await UserRepository(session).create(email="is03@example.com", password_hash="x" * 8)

    async with tenant_context(app_engine, tenant_id) as session:
        detail = await _build_service(session).get_investment_detail(uuid4())
    assert detail is None


# ---------------------------------------------------------------------------
# IS-04: lifecycle — create, update, delete
# ---------------------------------------------------------------------------


async def test_is04_lifecycle_create_update_delete(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    actor, ac = await _seed_actor_and_asset_class(app_engine, tenant_id, email="is04@example.com")

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        svc = _build_service(session)
        investment = await svc.create_investment(
            name="Lifecycle",
            investment_type="private_equity",
            asset_class_id=ac.id,
            currency="EUR",
            created_by=actor.id,
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        svc = _build_service(session)
        updated = await svc.update_investment(investment.id, name="Lifecycle Renamed")
        assert updated is not None
        assert updated.name == "Lifecycle Renamed"

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        svc = _build_service(session)
        deleted = await svc.delete_investment(investment.id)
        assert deleted is True
        deleted_again = await svc.delete_investment(investment.id)
        assert deleted_again is False


# ---------------------------------------------------------------------------
# IS-05: add_nav is UPSERT on (investment, date, kind)
# ---------------------------------------------------------------------------


async def test_is05_add_nav_upserts(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    actor, ac = await _seed_actor_and_asset_class(app_engine, tenant_id, email="is05@example.com")

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        svc = _build_service(session)
        investment = await svc.create_investment(
            name="UPSERT Fund",
            investment_type="private_equity",
            asset_class_id=ac.id,
            currency="EUR",
            created_by=actor.id,
        )
        first = await svc.add_nav(
            investment_id=investment.id,
            as_of_date=date(2025, 12, 31),
            nav_kind="actual",
            nav_value=Decimal("100"),
            currency="EUR",
            source=None,
            created_by=actor.id,
        )
        second = await svc.add_nav(
            investment_id=investment.id,
            as_of_date=date(2025, 12, 31),
            nav_kind="actual",
            nav_value=Decimal("200"),
            currency="EUR",
            source=None,
            created_by=actor.id,
        )

    assert first.id == second.id
    assert second.nav_value == Decimal("200.0000")


# ---------------------------------------------------------------------------
# IS-06: update_nav and delete_nav operate by id
# ---------------------------------------------------------------------------


async def test_is06_update_and_delete_nav_by_id(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    actor, ac = await _seed_actor_and_asset_class(app_engine, tenant_id, email="is06@example.com")

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        svc = _build_service(session)
        investment = await svc.create_investment(
            name="NAV-id Fund",
            investment_type="private_equity",
            asset_class_id=ac.id,
            currency="EUR",
            created_by=actor.id,
        )
        nav = await svc.add_nav(
            investment_id=investment.id,
            as_of_date=date(2025, 12, 31),
            nav_kind="actual",
            nav_value=Decimal("100"),
            currency="EUR",
            source=None,
            created_by=actor.id,
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        svc = _build_service(session)
        updated = await svc.update_nav(
            nav.id,
            nav_value=Decimal("150"),
            currency="EUR",
            source="corrected",
            created_by=actor.id,
        )
        assert updated is not None
        assert updated.nav_value == Decimal("150.0000")
        assert updated.source == "corrected"

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        svc = _build_service(session)
        deleted = await svc.delete_nav(nav.id)
        assert deleted is True
        deleted_again = await svc.delete_nav(nav.id)
        assert deleted_again is False


# ---------------------------------------------------------------------------
# IS-07: update_nav returns None for unknown id
# ---------------------------------------------------------------------------


async def test_is07_update_nav_missing_returns_none(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    async with tenant_context(app_engine, tenant_id) as session:
        actor = await UserRepository(session).create(
            email="is07@example.com", password_hash="x" * 8
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        svc = _build_service(session)
        updated = await svc.update_nav(
            uuid4(),
            nav_value=Decimal("1"),
            currency="EUR",
            source=None,
            created_by=actor.id,
        )
    assert updated is None


# ---------------------------------------------------------------------------
# IS-08: cashflow lifecycle — add, update, delete
# ---------------------------------------------------------------------------


async def test_is08_cashflow_lifecycle(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    actor, ac = await _seed_actor_and_asset_class(app_engine, tenant_id, email="is08@example.com")

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        svc = _build_service(session)
        investment = await svc.create_investment(
            name="CF Fund",
            investment_type="private_equity",
            asset_class_id=ac.id,
            currency="EUR",
            created_by=actor.id,
        )
        cashflow = await svc.add_cashflow(
            investment_id=investment.id,
            flow_timestamp=datetime(2025, 6, 15, 12, 0, tzinfo=timezone.utc),
            flow_type="capital_call",
            flow_kind="actual",
            amount=Decimal("-1000"),
            currency="EUR",
            description="Original",
            created_by=actor.id,
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        svc = _build_service(session)
        updated = await svc.update_cashflow(
            cashflow.id,
            acting_user=actor.id,
            amount=Decimal("-950"),
            description="Corrected",
        )
        assert updated is not None
        assert updated.amount == Decimal("-950.0000")
        assert updated.description == "Corrected"

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        svc = _build_service(session)
        deleted = await svc.delete_cashflow(cashflow.id, acting_user=actor.id)
        assert deleted is True
        deleted_again = await svc.delete_cashflow(cashflow.id, acting_user=actor.id)
        assert deleted_again is False


# ---------------------------------------------------------------------------
# IS-09: set_investment_active toggles soft-delete
# ---------------------------------------------------------------------------


async def test_is09_set_investment_active_toggles(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    actor, ac = await _seed_actor_and_asset_class(app_engine, tenant_id, email="is09@example.com")

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        svc = _build_service(session)
        investment = await svc.create_investment(
            name="Toggle Fund",
            investment_type="private_equity",
            asset_class_id=ac.id,
            currency="EUR",
            created_by=actor.id,
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        svc = _build_service(session)
        await svc.set_investment_active(investment.id, False)
    async with tenant_context(app_engine, tenant_id) as session:
        detail = await _build_service(session).get_investment_detail(investment.id)
    assert detail is not None
    assert detail.investment.is_active is False

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        svc = _build_service(session)
        await svc.set_investment_active(investment.id, True)
    async with tenant_context(app_engine, tenant_id) as session:
        detail = await _build_service(session).get_investment_detail(investment.id)
    assert detail is not None
    assert detail.investment.is_active is True
