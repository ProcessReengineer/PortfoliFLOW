# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""LimitsRepository tests against the live compose Postgres.

The ``limit_sets`` and ``limits`` tables are tenant-scoped
(RLS-policed). The repository's single writer
:meth:`LimitsRepository.create_set_with_limits` is transactional
within the caller's session — both rows commit or both roll back.

Coverage
--------
* LR-01: ``create_set_with_limits`` roundtrip; per-class rows are
  retrievable via ``list_limits``; ``get_effective_set`` and
  ``list_sets`` agree.
* LR-02: ``get_effective_set`` picks the latest set whose
  ``effective_from`` ≤ the query date (ADR-0056 §Selection).
* LR-03: UNIQUE ``(tenant_id, family, effective_from)`` raises on a
  duplicate.
* LR-04: CHECK ``family IN ('saa', 'anlv')`` blocks invalid family
  values.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from core.repositories import tenant_context
from core.repositories.limits_repository import LimitsRepository


async def _seed_user(superuser_engine: AsyncEngine, tenant_id: UUID) -> UUID:
    user_id = uuid4()
    async with superuser_engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.tenant_id', :tid, true)"),
            {"tid": str(tenant_id)},
        )
        await conn.execute(
            text(
                "INSERT INTO users "
                "(id, tenant_id, email, password_hash, roles, is_active) "
                "VALUES (:uid, :tid, :email, :hash, ARRAY['owner']::text[], TRUE)"
            ),
            {
                "uid": str(user_id),
                "tid": str(tenant_id),
                "email": f"u-{user_id}@example.com",
                "hash": "$2b$04$placeholder_hash_for_repository_tests_only",
            },
        )
    return user_id


# ---------------------------------------------------------------------------
# LR-01: roundtrip
# ---------------------------------------------------------------------------


async def test_lr01_create_set_with_limits_roundtrip(
    app_engine: AsyncEngine,
    superuser_engine: AsyncEngine,
    seed_tenant,
) -> None:
    tenant_id = await seed_tenant("LR-01")
    user_id = await _seed_user(superuser_engine, tenant_id)

    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        repo = LimitsRepository(session)
        created = await repo.create_set_with_limits(
            family="saa",
            effective_from=date(2024, 1, 1),
            label="SAA initial",
            notes="Section 3.2",
            limits={
                "equities": Decimal("25"),
                "real_estate": Decimal("10"),
                "private_equity": Decimal("10"),
            },
            created_by=user_id,
        )
        assert created.family == "saa"
        assert created.label == "SAA initial"

        all_sets = await repo.list_sets()
        assert [s.id for s in all_sets] == [created.id]

        limits = await repo.list_limits(created.id)
        assert {lim.class_key for lim in limits} == {
            "equities",
            "real_estate",
            "private_equity",
        }


# ---------------------------------------------------------------------------
# LR-02: effective-from resolution
# ---------------------------------------------------------------------------


async def test_lr02_get_effective_set_resolves_latest_at_or_before(
    app_engine: AsyncEngine,
    superuser_engine: AsyncEngine,
    seed_tenant,
) -> None:
    tenant_id = await seed_tenant("LR-02")
    user_id = await _seed_user(superuser_engine, tenant_id)

    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        repo = LimitsRepository(session)
        v1 = await repo.create_set_with_limits(
            family="saa",
            effective_from=date(2022, 1, 1),
            label="SAA 2022",
            notes=None,
            limits={"equities": Decimal("100")},
            created_by=user_id,
        )
        v2 = await repo.create_set_with_limits(
            family="saa",
            effective_from=date(2024, 7, 1),
            label="SAA 2024",
            notes=None,
            limits={"equities": Decimal("100")},
            created_by=user_id,
        )

        # Before v1 → None.
        assert await repo.get_effective_set("saa", date(2021, 12, 31)) is None
        # Exactly v1.effective_from → v1.
        eff_v1 = await repo.get_effective_set("saa", date(2022, 1, 1))
        assert eff_v1 is not None and eff_v1.id == v1.id
        # Between v1 and v2 → v1.
        eff_mid = await repo.get_effective_set("saa", date(2024, 6, 30))
        assert eff_mid is not None and eff_mid.id == v1.id
        # On / after v2 → v2.
        eff_v2 = await repo.get_effective_set("saa", date(2025, 1, 1))
        assert eff_v2 is not None and eff_v2.id == v2.id

        # The other family has no rows → None for any date.
        assert await repo.get_effective_set("anlv", date(2025, 1, 1)) is None


# ---------------------------------------------------------------------------
# LR-03: UNIQUE (tenant_id, family, effective_from)
# ---------------------------------------------------------------------------


async def test_lr03_unique_constraint_blocks_duplicate(
    app_engine: AsyncEngine,
    superuser_engine: AsyncEngine,
    seed_tenant,
) -> None:
    tenant_id = await seed_tenant("LR-03")
    user_id = await _seed_user(superuser_engine, tenant_id)

    with pytest.raises(IntegrityError):
        async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
            repo = LimitsRepository(session)
            await repo.create_set_with_limits(
                family="saa",
                effective_from=date(2024, 1, 1),
                label="first",
                notes=None,
                limits={"equities": Decimal("100")},
                created_by=user_id,
            )
            await repo.create_set_with_limits(
                family="saa",
                effective_from=date(2024, 1, 1),  # same date — duplicate
                label="second",
                notes=None,
                limits={"equities": Decimal("100")},
                created_by=user_id,
            )


# ---------------------------------------------------------------------------
# LR-04: CHECK family IN ('saa', 'anlv')
# ---------------------------------------------------------------------------


async def test_lr04_check_family_rejects_invalid_value(
    app_engine: AsyncEngine,
    superuser_engine: AsyncEngine,
    seed_tenant,
) -> None:
    tenant_id = await seed_tenant("LR-04")
    user_id = await _seed_user(superuser_engine, tenant_id)

    with pytest.raises(IntegrityError):
        async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
            repo = LimitsRepository(session)
            await repo.create_set_with_limits(
                family="satzung",  # old placeholder — must be rejected
                effective_from=date(2024, 1, 1),
                label="x",
                notes=None,
                limits={"equities": Decimal("100")},
                created_by=user_id,
            )
