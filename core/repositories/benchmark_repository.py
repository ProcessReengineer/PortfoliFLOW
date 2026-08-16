# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""BenchmarkRepository — persistence for the per-tenant benchmark catalogue.

Backs the ``benchmarks`` table introduced in migration b011 (per
ADR-0061 §Decision). Shape mirrors the Phase-3/4 repositories: a
tenant-scoped :class:`AsyncSession` is passed in, methods return
frozen DTOs, ``tenant_id`` is implicit in the session context
(RLS WITH CHECK derives it from ``app.tenant_id``).

The :meth:`upsert_by_code` method is the idempotent re-import seam
the Excel-import path uses: re-running the same workbook updates
``display_name``, ``description``, and ``provider_hint`` on the
existing row by tenant-scoped ``code``. ``created_by`` and
``created_at`` are preserved across upserts; ``updated_at`` is
bumped to ``NOW()``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from core.models.benchmark import Benchmark
from core.repositories.base import BaseRepository


@dataclass(frozen=True)
class BenchmarkDTO:
    """Plain data-only view of a ``benchmarks`` row."""

    id: UUID
    tenant_id: UUID
    code: str
    display_name: str
    description: str | None
    provider_hint: str | None
    created_by: UUID
    created_at: datetime
    updated_at: datetime


def _to_dto(model: Benchmark) -> BenchmarkDTO:
    return BenchmarkDTO(
        id=model.id,
        tenant_id=model.tenant_id,
        code=model.code,
        display_name=model.display_name,
        description=model.description,
        provider_hint=model.provider_hint,
        created_by=model.created_by,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


class BenchmarkRepository(BaseRepository):
    """Read and write benchmark definitions in the active tenant context."""

    async def create(
        self,
        code: str,
        display_name: str,
        description: str | None,
        provider_hint: str | None,
        *,
        created_by: UUID,
    ) -> BenchmarkDTO:
        """Create a new benchmark in the current tenant context.

        Args:
            code: Tenant-unique short identifier (e.g.
                ``"BM_EQUITIES_DM"``).
            display_name: Human-readable label rendered in the
                Benchmarks & Attribution section.
            description: Optional operator-facing description.
            provider_hint: Optional provenance note documenting the
                intended external data source.
            created_by: UUID of the user creating the row.

        Returns:
            The newly created :class:`BenchmarkDTO`.
        """
        tenant_row = await self._session.execute(
            text("SELECT current_setting('app.tenant_id')::uuid AS tid")
        )
        active_tenant: UUID = tenant_row.scalar_one()

        model = Benchmark(
            tenant_id=active_tenant,
            code=code,
            display_name=display_name,
            description=description,
            provider_hint=provider_hint,
            created_by=created_by,
        )
        self._session.add(model)
        await self._session.flush()
        await self._session.refresh(model)
        return _to_dto(model)

    async def upsert_by_code(
        self,
        code: str,
        display_name: str,
        description: str | None,
        provider_hint: str | None,
        *,
        created_by: UUID,
    ) -> BenchmarkDTO:
        """Insert or refresh a benchmark by its tenant-scoped ``code``.

        On conflict (``(tenant_id, code)`` already exists), the row's
        ``display_name``, ``description``, and ``provider_hint`` are
        overwritten with the supplied values; ``created_by`` and
        ``created_at`` are preserved. ``updated_at`` bumps to
        ``NOW()``.

        This is the idempotent re-import seam used by
        :meth:`InvestmentService.transform_benchmarks_from_upload`:
        re-running the same Excel workbook produces the same DB
        state without IntegrityError noise.

        Args:
            code: Tenant-unique short identifier.
            display_name: Refreshed display label.
            description: Refreshed description.
            provider_hint: Refreshed provider hint.
            created_by: UUID of the user persisting this row. Used
                only on first-time inserts; preserved across updates.

        Returns:
            The :class:`BenchmarkDTO` after the upsert.
        """
        tenant_row = await self._session.execute(
            text("SELECT current_setting('app.tenant_id')::uuid AS tid")
        )
        active_tenant: UUID = tenant_row.scalar_one()

        stmt = pg_insert(Benchmark).values(
            tenant_id=active_tenant,
            code=code,
            display_name=display_name,
            description=description,
            provider_hint=provider_hint,
            created_by=created_by,
        )
        stmt = stmt.on_conflict_do_update(
            constraint="uq_benchmarks_tenant_code",
            set_={
                "display_name": stmt.excluded.display_name,
                "description": stmt.excluded.description,
                "provider_hint": stmt.excluded.provider_hint,
                "updated_at": text("NOW()"),
            },
        )
        await self._session.execute(stmt)
        await self._session.flush()

        refreshed = await self.get_by_code(code)
        # The active tenant context guarantees the just-upserted row
        # is visible; ``None`` would imply an RLS misconfiguration.
        assert refreshed is not None, "upsert_by_code: post-upsert lookup miss"
        return refreshed

    async def get_by_id(self, benchmark_id: UUID) -> BenchmarkDTO | None:
        """Return the benchmark with the given id, or ``None`` if absent.

        Cross-tenant rows are invisible (RLS hides them); the
        repository correctly reports absence rather than raising.
        """
        result = await self._session.execute(select(Benchmark).where(Benchmark.id == benchmark_id))
        model = result.scalar_one_or_none()
        return _to_dto(model) if model is not None else None

    async def get_by_code(self, code: str) -> BenchmarkDTO | None:
        """Resolve a benchmark by its tenant-scoped ``code``, or ``None``.

        Args:
            code: The tenant-scoped benchmark code (e.g.
                ``"BM_EQUITIES_DM"``). Empty input always misses
                (returns ``None``).

        Returns:
            The matching :class:`BenchmarkDTO` if found in the
            active tenant, otherwise ``None``.
        """
        if not isinstance(code, str):
            return None
        normalised = code.strip()
        if not normalised:
            return None
        result = await self._session.execute(select(Benchmark).where(Benchmark.code == normalised))
        model = result.scalar_one_or_none()
        return _to_dto(model) if model is not None else None

    async def list_all(self) -> list[BenchmarkDTO]:
        """Return every benchmark visible in the active tenant context.

        Sorted by ``display_name`` for stable rendering.
        """
        result = await self._session.execute(select(Benchmark).order_by(Benchmark.display_name))
        return [_to_dto(model) for model in result.scalars().all()]
