# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""``UserRepository.set_active`` / ``set_roles`` against the live Postgres.

The two ADR-0121 §5 additions. They run as the unprivileged
``portfoliflow_app`` role like the rest of the repository suite, so the
"foreign id" cases exercise real RLS filtering rather than a Python-side
tenant check.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from core.repositories import UserRepository, tenant_context


# ---------------------------------------------------------------------------
# set_active
# ---------------------------------------------------------------------------


async def test_set_active_round_trips(app_engine: AsyncEngine, seed_tenant) -> None:
    """Deactivation and reactivation both persist and read back."""
    tenant_id = await seed_tenant()
    async with tenant_context(app_engine, tenant_id) as session:
        created = await UserRepository(session).create(
            email="toggle@example.com", password_hash="x" * 8
        )
    assert created.is_active is True

    async with tenant_context(app_engine, tenant_id) as session:
        deactivated = await UserRepository(session).set_active(created.id, False)
    assert deactivated is not None
    assert deactivated.is_active is False
    assert deactivated.id == created.id

    # The returned DTO is not just an optimistic echo — a fresh session
    # sees the same state.
    async with tenant_context(app_engine, tenant_id) as session:
        roundtrip = await UserRepository(session).get_by_id(created.id)
    assert roundtrip is not None
    assert roundtrip.is_active is False

    async with tenant_context(app_engine, tenant_id) as session:
        reactivated = await UserRepository(session).set_active(created.id, True)
    assert reactivated is not None
    assert reactivated.is_active is True


async def test_set_active_returns_none_for_unknown_id(app_engine: AsyncEngine, seed_tenant) -> None:
    """An id that matches no row reports absence rather than raising."""
    from uuid import uuid4

    tenant_id = await seed_tenant()
    async with tenant_context(app_engine, tenant_id) as session:
        assert await UserRepository(session).set_active(uuid4(), False) is None


async def test_set_active_returns_none_for_foreign_tenant_id(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """A user of another tenant is invisible — and stays untouched."""
    tenant_a = await seed_tenant(name="Tenant A")
    tenant_b = await seed_tenant(name="Tenant B")

    async with tenant_context(app_engine, tenant_b) as session:
        foreign = await UserRepository(session).create(
            email="foreign@example.com", password_hash="x" * 8
        )

    async with tenant_context(app_engine, tenant_a) as session:
        assert await UserRepository(session).set_active(foreign.id, False) is None

    async with tenant_context(app_engine, tenant_b) as session:
        untouched = await UserRepository(session).get_by_id(foreign.id)
    assert untouched is not None
    assert untouched.is_active is True


# ---------------------------------------------------------------------------
# set_roles
# ---------------------------------------------------------------------------


async def test_set_roles_round_trips(app_engine: AsyncEngine, seed_tenant) -> None:
    """The role set is replaced wholesale and reads back."""
    tenant_id = await seed_tenant()
    async with tenant_context(app_engine, tenant_id) as session:
        created = await UserRepository(session).create(
            email="promote@example.com", password_hash="x" * 8, roles=["member"]
        )
    assert created.roles == ("member",)

    async with tenant_context(app_engine, tenant_id) as session:
        promoted = await UserRepository(session).set_roles(created.id, ["owner"])
    assert promoted is not None
    assert promoted.roles == ("owner",)

    async with tenant_context(app_engine, tenant_id) as session:
        roundtrip = await UserRepository(session).get_by_id(created.id)
    assert roundtrip is not None
    assert roundtrip.roles == ("owner",)


async def test_set_roles_rejects_unknown_role(app_engine: AsyncEngine, seed_tenant) -> None:
    """An unknown role raises before the database sees it."""
    tenant_id = await seed_tenant()
    async with tenant_context(app_engine, tenant_id) as session:
        created = await UserRepository(session).create(
            email="badrole@example.com", password_hash="x" * 8
        )

    async with tenant_context(app_engine, tenant_id) as session:
        with pytest.raises(ValueError, match="unknown role"):
            await UserRepository(session).set_roles(created.id, ["superuser"])

    async with tenant_context(app_engine, tenant_id) as session:
        unchanged = await UserRepository(session).get_by_id(created.id)
    assert unchanged is not None
    assert unchanged.roles == ("member",)


async def test_set_roles_rejects_empty_role_set(app_engine: AsyncEngine, seed_tenant) -> None:
    """An empty role set is refused — every user holds at least one role."""
    tenant_id = await seed_tenant()
    async with tenant_context(app_engine, tenant_id) as session:
        created = await UserRepository(session).create(
            email="noroles@example.com", password_hash="x" * 8
        )

    async with tenant_context(app_engine, tenant_id) as session:
        with pytest.raises(ValueError, match="non-empty"):
            await UserRepository(session).set_roles(created.id, [])


async def test_set_roles_returns_none_for_foreign_tenant_id(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """A user of another tenant is invisible — and keeps their roles."""
    tenant_a = await seed_tenant(name="Tenant A")
    tenant_b = await seed_tenant(name="Tenant B")

    async with tenant_context(app_engine, tenant_b) as session:
        foreign = await UserRepository(session).create(
            email="foreign-roles@example.com", password_hash="x" * 8, roles=["member"]
        )

    async with tenant_context(app_engine, tenant_a) as session:
        assert await UserRepository(session).set_roles(foreign.id, ["owner"]) is None

    async with tenant_context(app_engine, tenant_b) as session:
        untouched = await UserRepository(session).get_by_id(foreign.id)
    assert untouched is not None
    assert untouched.roles == ("member",)
