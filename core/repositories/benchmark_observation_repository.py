# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""BenchmarkObservationRepository — persistence for benchmark period-return series.

Backs the ``benchmark_observations`` table introduced in migration
b011 (per ADR-0061 §Decision). Each row is one ``(benchmark_id,
as_of_date, period_return)`` triple. Tenant-scoped via the
denormalised ``tenant_id`` column; RLS evaluates row-locally.

The :meth:`replace_observations_for_benchmark` writer implements
the idempotent re-import contract used by the Excel-import path:
DELETE every existing observation for the benchmark, then INSERT
the new generation in one transaction.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date as _date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import delete as sa_delete, select, text

from core.models.benchmark_observation import BenchmarkObservation
from core.repositories.base import BaseRepository


@dataclass(frozen=True)
class BenchmarkObservationDTO:
    """Plain data-only view of a ``benchmark_observations`` row."""

    id: UUID
    tenant_id: UUID
    benchmark_id: UUID
    as_of_date: _date
    period_return: Decimal
    created_at: datetime


def _to_dto(model: BenchmarkObservation) -> BenchmarkObservationDTO:
    return BenchmarkObservationDTO(
        id=model.id,
        tenant_id=model.tenant_id,
        benchmark_id=model.benchmark_id,
        as_of_date=model.as_of_date,
        period_return=model.period_return,
        created_at=model.created_at,
    )


class BenchmarkObservationRepository(BaseRepository):
    """Read and write benchmark observations in the active tenant context."""

    async def replace_observations_for_benchmark(
        self,
        benchmark_id: UUID,
        observations: list[tuple[_date, Decimal]],
    ) -> int:
        """Atomically replace all observations for one benchmark.

        Deletes every existing row for ``benchmark_id`` in the active
        tenant, then inserts the supplied ``(as_of_date,
        period_return)`` pairs. Both operations land in one
        transaction-flush cycle so a failure leaves the previous
        generation in place.

        Args:
            benchmark_id: The benchmark whose observation history is
                being replaced.
            observations: New ``(as_of_date, period_return)`` pairs.
                An empty list deletes every existing observation for
                the benchmark.

        Returns:
            The number of newly inserted rows.
        """
        tenant_row = await self._session.execute(
            text("SELECT current_setting('app.tenant_id')::uuid AS tid")
        )
        active_tenant: UUID = tenant_row.scalar_one()

        await self._session.execute(
            sa_delete(BenchmarkObservation).where(BenchmarkObservation.benchmark_id == benchmark_id)
        )
        if not observations:
            await self._session.flush()
            return 0

        # Batch the bulk insert. ``benchmark_observations`` has six
        # bound parameters per row (id is generated server-side and
        # ``created_at`` defaults to NOW()) — Postgres caps a single
        # statement at 32 767 bound parameters; 5000 rows × 4 params
        # leaves comfortable headroom.
        _BATCH = 5000
        inserted = 0
        for start in range(0, len(observations), _BATCH):
            chunk = observations[start : start + _BATCH]
            self._session.add_all(
                [
                    BenchmarkObservation(
                        tenant_id=active_tenant,
                        benchmark_id=benchmark_id,
                        as_of_date=as_of,
                        period_return=ret,
                    )
                    for as_of, ret in chunk
                ]
            )
            inserted += len(chunk)
        await self._session.flush()
        return inserted

    async def list_for_benchmark(
        self,
        benchmark_id: UUID,
        from_date: _date | None = None,
        to_date: _date | None = None,
    ) -> list[BenchmarkObservationDTO]:
        """Return observations for one benchmark in the active tenant.

        Args:
            benchmark_id: The benchmark to load.
            from_date: Inclusive lower bound; ``None`` means
                unbounded.
            to_date: Inclusive upper bound; ``None`` means unbounded.

        Returns:
            The matching observations, oldest first.
        """
        stmt = select(BenchmarkObservation).where(BenchmarkObservation.benchmark_id == benchmark_id)
        if from_date is not None:
            stmt = stmt.where(BenchmarkObservation.as_of_date >= from_date)
        if to_date is not None:
            stmt = stmt.where(BenchmarkObservation.as_of_date <= to_date)
        stmt = stmt.order_by(BenchmarkObservation.as_of_date)
        result = await self._session.execute(stmt)
        return [_to_dto(model) for model in result.scalars().all()]

    async def list_for_benchmarks(
        self,
        benchmark_ids: list[UUID],
        from_date: _date | None = None,
        to_date: _date | None = None,
    ) -> list[BenchmarkObservationDTO]:
        """Return observations for multiple benchmarks in one query.

        Plural variant that avoids N+1 queries when the caller needs
        observations for several benchmarks (e.g. the analytics layer
        loading all asset-class benchmarks at once).

        Args:
            benchmark_ids: The benchmarks to load. Empty list returns
                an empty list.
            from_date: Inclusive lower bound; ``None`` means
                unbounded.
            to_date: Inclusive upper bound; ``None`` means unbounded.

        Returns:
            All matching observations, ordered by
            ``(benchmark_id, as_of_date)``.
        """
        if not benchmark_ids:
            return []
        stmt = select(BenchmarkObservation).where(
            BenchmarkObservation.benchmark_id.in_(benchmark_ids)
        )
        if from_date is not None:
            stmt = stmt.where(BenchmarkObservation.as_of_date >= from_date)
        if to_date is not None:
            stmt = stmt.where(BenchmarkObservation.as_of_date <= to_date)
        stmt = stmt.order_by(
            BenchmarkObservation.benchmark_id,
            BenchmarkObservation.as_of_date,
        )
        result = await self._session.execute(stmt)
        return [_to_dto(model) for model in result.scalars().all()]
