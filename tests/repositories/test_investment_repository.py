# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""InvestmentRepository tests against the live compose Postgres.

Each test runs as the unprivileged ``portfoliflow_app`` role so RLS
evaluates exactly as it will in production. Tenant creation goes
through the ``seed_tenant`` superuser fixture.

Coverage:

* Round-trip create + read by id, by name, and by type.
* ``list_all`` and ``list_active`` honour ordering and the
  ``is_active`` filter.
* ``update`` modifies only the requested fields.
* ``set_active`` toggles the soft-delete flag.
* ``delete`` removes the row and reports rowcount.
* RLS isolates investments between tenants.
* Unique-constraint conflict surfaces on duplicate ``(tenant_id, name)``.
* CHECK constraint rejects invalid ``investment_type`` values.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from core.repositories import (
    AssetClassRepository,
    InvestmentRepository,
    UserRepository,
    tenant_context,
)


async def _make_actor_and_asset_class(
    app_engine: AsyncEngine,
    tenant_id,
    *,
    email: str,
    code: str = "default_class",
    display_name: str = "Default Class",
):
    """Common fixture work: create an actor user and one asset class."""
    async with tenant_context(app_engine, tenant_id) as session:
        actor = await UserRepository(session).create(email=email, password_hash="x" * 8)
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        asset_class = await AssetClassRepository(session).create(
            code=code, display_name=display_name
        )
    return actor, asset_class


# ---------------------------------------------------------------------------
# IR-01: round-trip create + read
# ---------------------------------------------------------------------------


async def test_ir01_create_and_get_by_id(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    actor, asset_class = await _make_actor_and_asset_class(
        app_engine, tenant_id, email="ir01@example.com"
    )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        repo = InvestmentRepository(session)
        created = await repo.create(
            name="Permira VII",
            investment_type="private_equity",
            asset_class_id=asset_class.id,
            currency="EUR",
            created_by=actor.id,
            manager_name="Permira",
            region="EU",
            vintage_year=2024,
            commitment_amount=Decimal("10000000.0000"),
        )

    assert created.name == "Permira VII"
    assert created.investment_type == "private_equity"
    assert created.tenant_id == tenant_id
    assert created.asset_class_id == asset_class.id
    assert created.is_active is True
    assert created.commitment_amount == Decimal("10000000.0000")

    async with tenant_context(app_engine, tenant_id) as session:
        fetched = await InvestmentRepository(session).get_by_id(created.id)
    assert fetched is not None
    assert fetched.id == created.id
    assert fetched.manager_name == "Permira"


# ---------------------------------------------------------------------------
# IR-02: get_by_name resolves the natural key
# ---------------------------------------------------------------------------


async def test_ir02_get_by_name_returns_match(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    actor, asset_class = await _make_actor_and_asset_class(
        app_engine, tenant_id, email="ir02@example.com"
    )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        await InvestmentRepository(session).create(
            name="Carlyle Asia IV",
            investment_type="private_equity",
            asset_class_id=asset_class.id,
            currency="USD",
            created_by=actor.id,
        )

    async with tenant_context(app_engine, tenant_id) as session:
        repo = InvestmentRepository(session)
        match = await repo.get_by_name("Carlyle Asia IV")
        miss = await repo.get_by_name("Not Real Fund")

    assert match is not None
    assert match.investment_type == "private_equity"
    assert miss is None


# ---------------------------------------------------------------------------
# IR-03: list_all orders by name; list_active filters out is_active=FALSE
# ---------------------------------------------------------------------------


async def test_ir03_list_all_and_list_active(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    actor, asset_class = await _make_actor_and_asset_class(
        app_engine, tenant_id, email="ir03@example.com"
    )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        repo = InvestmentRepository(session)
        await repo.create(
            name="Zeta Fund",
            investment_type="private_equity",
            asset_class_id=asset_class.id,
            currency="EUR",
            created_by=actor.id,
        )
        await repo.create(
            name="Alpha Fund",
            investment_type="private_equity",
            asset_class_id=asset_class.id,
            currency="EUR",
            created_by=actor.id,
        )
        gamma = await repo.create(
            name="Gamma Fund",
            investment_type="private_equity",
            asset_class_id=asset_class.id,
            currency="EUR",
            created_by=actor.id,
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        await InvestmentRepository(session).set_active(gamma.id, False)

    async with tenant_context(app_engine, tenant_id) as session:
        all_investments = await InvestmentRepository(session).list_all()
        active_only = await InvestmentRepository(session).list_active()

    assert [i.name for i in all_investments] == [
        "Alpha Fund",
        "Gamma Fund",
        "Zeta Fund",
    ]
    assert [i.name for i in active_only] == ["Alpha Fund", "Zeta Fund"]


# ---------------------------------------------------------------------------
# IR-04: list_by_type filters on investment_type
# ---------------------------------------------------------------------------


async def test_ir04_list_by_type(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    actor, asset_class = await _make_actor_and_asset_class(
        app_engine, tenant_id, email="ir04@example.com"
    )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        repo = InvestmentRepository(session)
        await repo.create(
            name="PE Fund",
            investment_type="private_equity",
            asset_class_id=asset_class.id,
            currency="EUR",
            created_by=actor.id,
        )
        await repo.create(
            name="RE Fund",
            investment_type="real_estate",
            asset_class_id=asset_class.id,
            currency="EUR",
            created_by=actor.id,
        )

    async with tenant_context(app_engine, tenant_id) as session:
        repo = InvestmentRepository(session)
        pe_only = await repo.list_by_type("private_equity")
        re_only = await repo.list_by_type("real_estate")

    assert [i.name for i in pe_only] == ["PE Fund"]
    assert [i.name for i in re_only] == ["RE Fund"]


# ---------------------------------------------------------------------------
# IR-05: update modifies only the requested fields
# ---------------------------------------------------------------------------


async def test_ir05_update_modifies_requested_fields(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    actor, asset_class = await _make_actor_and_asset_class(
        app_engine, tenant_id, email="ir05@example.com"
    )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        repo = InvestmentRepository(session)
        created = await repo.create(
            name="Original Name",
            investment_type="private_equity",
            asset_class_id=asset_class.id,
            currency="EUR",
            created_by=actor.id,
            manager_name="Original Manager",
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        updated = await InvestmentRepository(session).update(
            created.id, name="Renamed", region="DACH"
        )

    assert updated is not None
    assert updated.name == "Renamed"
    assert updated.region == "DACH"
    assert updated.manager_name == "Original Manager"  # untouched
    assert updated.currency == "EUR"  # untouched


# ---------------------------------------------------------------------------
# IR-06: set_active toggles the soft-delete flag
# ---------------------------------------------------------------------------


async def test_ir06_set_active_toggles(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    actor, asset_class = await _make_actor_and_asset_class(
        app_engine, tenant_id, email="ir06@example.com"
    )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        repo = InvestmentRepository(session)
        created = await repo.create(
            name="To Toggle",
            investment_type="private_equity",
            asset_class_id=asset_class.id,
            currency="EUR",
            created_by=actor.id,
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        await InvestmentRepository(session).set_active(created.id, False)
    async with tenant_context(app_engine, tenant_id) as session:
        after_deactivate = await InvestmentRepository(session).get_by_id(created.id)
    assert after_deactivate is not None
    assert after_deactivate.is_active is False

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        await InvestmentRepository(session).set_active(created.id, True)
    async with tenant_context(app_engine, tenant_id) as session:
        after_reactivate = await InvestmentRepository(session).get_by_id(created.id)
    assert after_reactivate is not None
    assert after_reactivate.is_active is True


# ---------------------------------------------------------------------------
# IR-07: delete removes the row and returns True only when present
# ---------------------------------------------------------------------------


async def test_ir07_delete_returns_rowcount_signal(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    actor, asset_class = await _make_actor_and_asset_class(
        app_engine, tenant_id, email="ir07@example.com"
    )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        repo = InvestmentRepository(session)
        created = await repo.create(
            name="To Delete",
            investment_type="private_equity",
            asset_class_id=asset_class.id,
            currency="EUR",
            created_by=actor.id,
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        deleted_first = await InvestmentRepository(session).delete(created.id)
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        deleted_second = await InvestmentRepository(session).delete(created.id)

    assert deleted_first is True
    assert deleted_second is False

    async with tenant_context(app_engine, tenant_id) as session:
        gone = await InvestmentRepository(session).get_by_id(created.id)
    assert gone is None


# ---------------------------------------------------------------------------
# IR-08: cross-tenant isolation
# ---------------------------------------------------------------------------


async def test_ir08_cross_tenant_isolation(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_a = await seed_tenant(name="Tenant A")
    tenant_b = await seed_tenant(name="Tenant B")

    actor_a, asset_class_a = await _make_actor_and_asset_class(
        app_engine, tenant_a, email="a@example.com", code="ac_a"
    )
    actor_b, asset_class_b = await _make_actor_and_asset_class(
        app_engine, tenant_b, email="b@example.com", code="ac_b"
    )

    async with tenant_context(app_engine, tenant_a, user_id=actor_a.id) as session:
        await InvestmentRepository(session).create(
            name="Shared Name",
            investment_type="private_equity",
            asset_class_id=asset_class_a.id,
            currency="EUR",
            created_by=actor_a.id,
        )
    async with tenant_context(app_engine, tenant_b, user_id=actor_b.id) as session:
        await InvestmentRepository(session).create(
            name="Shared Name",
            investment_type="real_estate",
            asset_class_id=asset_class_b.id,
            currency="USD",
            created_by=actor_b.id,
        )

    async with tenant_context(app_engine, tenant_a) as session:
        a_view = await InvestmentRepository(session).list_all()
    async with tenant_context(app_engine, tenant_b) as session:
        b_view = await InvestmentRepository(session).list_all()

    assert [i.investment_type for i in a_view] == ["private_equity"]
    assert [i.investment_type for i in b_view] == ["real_estate"]


# ---------------------------------------------------------------------------
# IR-09: duplicate (tenant_id, name) raises IntegrityError
# ---------------------------------------------------------------------------


async def test_ir09_duplicate_name_in_same_tenant_raises(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant()
    actor, asset_class = await _make_actor_and_asset_class(
        app_engine, tenant_id, email="ir09@example.com"
    )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        await InvestmentRepository(session).create(
            name="Duplicate Fund",
            investment_type="private_equity",
            asset_class_id=asset_class.id,
            currency="EUR",
            created_by=actor.id,
        )

    with pytest.raises(IntegrityError):
        async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
            await InvestmentRepository(session).create(
                name="Duplicate Fund",
                investment_type="real_estate",
                asset_class_id=asset_class.id,
                currency="EUR",
                created_by=actor.id,
            )


# ---------------------------------------------------------------------------
# IR-10: invalid investment_type rejected by CHECK constraint
# ---------------------------------------------------------------------------


async def test_ir10_invalid_investment_type_raises(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    actor, asset_class = await _make_actor_and_asset_class(
        app_engine, tenant_id, email="ir10@example.com"
    )

    with pytest.raises(IntegrityError):
        async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
            await InvestmentRepository(session).create(
                name="Bad Type Fund",
                investment_type="not_a_real_type",
                asset_class_id=asset_class.id,
                currency="EUR",
                created_by=actor.id,
            )
