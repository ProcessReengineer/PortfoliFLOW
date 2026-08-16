# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""BenchmarkObservationRepository tests against the live compose Postgres.

Each test runs as ``portfoliflow_app`` so RLS evaluates exactly as
in production. Coverage:

* ``replace_observations_for_benchmark`` is atomic (delete-all +
  insert-all in one flush).
* ``list_for_benchmark`` honours optional date filters.
* ``list_for_benchmarks`` returns concatenated, ordered observations.
* Cross-tenant isolation — observations are invisible across tenants.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncEngine

from core.repositories import (
    BenchmarkObservationRepository,
    BenchmarkRepository,
    UserRepository,
    tenant_context,
)


async def _make_benchmark(app_engine: AsyncEngine, tenant_id, actor_id, code: str):
    async with tenant_context(app_engine, tenant_id, user_id=actor_id) as session:
        return await BenchmarkRepository(session).create(
            code=code,
            display_name=code,
            description=None,
            provider_hint=None,
            created_by=actor_id,
        )


async def test_bo01_replace_inserts_observations(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    async with tenant_context(app_engine, tenant_id) as session:
        actor = await UserRepository(session).create(
            email="bo01@example.com", password_hash="x" * 8
        )
    bm = await _make_benchmark(app_engine, tenant_id, actor.id, "BM_A")

    obs = [
        (date(2026, 1, 1), Decimal("0.001")),
        (date(2026, 1, 2), Decimal("-0.002")),
        (date(2026, 1, 3), Decimal("0.003")),
    ]
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        repo = BenchmarkObservationRepository(session)
        inserted = await repo.replace_observations_for_benchmark(
            benchmark_id=bm.id, observations=obs
        )
    assert inserted == 3

    async with tenant_context(app_engine, tenant_id) as session:
        rows = await BenchmarkObservationRepository(session).list_for_benchmark(bm.id)
    assert [r.as_of_date for r in rows] == [
        date(2026, 1, 1),
        date(2026, 1, 2),
        date(2026, 1, 3),
    ]
    assert rows[1].period_return == Decimal("-0.0020000000")


async def test_bo02_replace_deletes_previous_generation(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant()
    async with tenant_context(app_engine, tenant_id) as session:
        actor = await UserRepository(session).create(
            email="bo02@example.com", password_hash="x" * 8
        )
    bm = await _make_benchmark(app_engine, tenant_id, actor.id, "BM_B")

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        repo = BenchmarkObservationRepository(session)
        await repo.replace_observations_for_benchmark(
            benchmark_id=bm.id,
            observations=[
                (date(2026, 1, 1), Decimal("0.1")),
                (date(2026, 1, 2), Decimal("0.2")),
            ],
        )
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        repo = BenchmarkObservationRepository(session)
        # New generation — only one row this time.
        inserted = await repo.replace_observations_for_benchmark(
            benchmark_id=bm.id,
            observations=[(date(2026, 2, 1), Decimal("0.5"))],
        )

    assert inserted == 1
    async with tenant_context(app_engine, tenant_id) as session:
        rows = await BenchmarkObservationRepository(session).list_for_benchmark(bm.id)
    assert len(rows) == 1
    assert rows[0].as_of_date == date(2026, 2, 1)


async def test_bo03_list_for_benchmark_date_bounds(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    async with tenant_context(app_engine, tenant_id) as session:
        actor = await UserRepository(session).create(
            email="bo03@example.com", password_hash="x" * 8
        )
    bm = await _make_benchmark(app_engine, tenant_id, actor.id, "BM_C")

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        repo = BenchmarkObservationRepository(session)
        await repo.replace_observations_for_benchmark(
            benchmark_id=bm.id,
            observations=[
                (date(2026, 1, 1), Decimal("0.1")),
                (date(2026, 2, 1), Decimal("0.2")),
                (date(2026, 3, 1), Decimal("0.3")),
                (date(2026, 4, 1), Decimal("0.4")),
            ],
        )

    async with tenant_context(app_engine, tenant_id) as session:
        repo = BenchmarkObservationRepository(session)
        bounded = await repo.list_for_benchmark(
            bm.id,
            from_date=date(2026, 2, 1),
            to_date=date(2026, 3, 1),
        )
    assert [r.as_of_date for r in bounded] == [
        date(2026, 2, 1),
        date(2026, 3, 1),
    ]


async def test_bo04_list_for_benchmarks_returns_combined(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant()
    async with tenant_context(app_engine, tenant_id) as session:
        actor = await UserRepository(session).create(
            email="bo04@example.com", password_hash="x" * 8
        )
    bm_a = await _make_benchmark(app_engine, tenant_id, actor.id, "BM_X")
    bm_b = await _make_benchmark(app_engine, tenant_id, actor.id, "BM_Y")

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        repo = BenchmarkObservationRepository(session)
        await repo.replace_observations_for_benchmark(
            benchmark_id=bm_a.id,
            observations=[(date(2026, 1, 1), Decimal("0.1"))],
        )
        await repo.replace_observations_for_benchmark(
            benchmark_id=bm_b.id,
            observations=[(date(2026, 1, 2), Decimal("0.2"))],
        )

    async with tenant_context(app_engine, tenant_id) as session:
        repo = BenchmarkObservationRepository(session)
        combined = await repo.list_for_benchmarks([bm_a.id, bm_b.id])
        empty = await repo.list_for_benchmarks([])
    assert len(combined) == 2
    assert {r.benchmark_id for r in combined} == {bm_a.id, bm_b.id}
    assert empty == []


async def test_bo05_cross_tenant_isolation(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_a = await seed_tenant(name="A")
    tenant_b = await seed_tenant(name="B")
    async with tenant_context(app_engine, tenant_a) as session:
        actor_a = await UserRepository(session).create(
            email="bo05a@example.com", password_hash="x" * 8
        )
    async with tenant_context(app_engine, tenant_b) as session:
        actor_b = await UserRepository(session).create(
            email="bo05b@example.com", password_hash="x" * 8
        )

    bm_a = await _make_benchmark(app_engine, tenant_a, actor_a.id, "BM")
    bm_b = await _make_benchmark(app_engine, tenant_b, actor_b.id, "BM")

    async with tenant_context(app_engine, tenant_a, user_id=actor_a.id) as session:
        await BenchmarkObservationRepository(session).replace_observations_for_benchmark(
            benchmark_id=bm_a.id,
            observations=[(date(2026, 1, 1), Decimal("0.1"))],
        )

    # Tenant B must not see Tenant A's observations even when
    # looking up Tenant A's benchmark id directly.
    async with tenant_context(app_engine, tenant_b) as session:
        repo = BenchmarkObservationRepository(session)
        cross = await repo.list_for_benchmark(bm_a.id)
        own = await repo.list_for_benchmark(bm_b.id)
    assert cross == []
    assert own == []
