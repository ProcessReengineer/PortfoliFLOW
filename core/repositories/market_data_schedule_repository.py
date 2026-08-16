# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""MarketDataScheduleRepository — persistence for live-import cadence.

Backs the ``market_data_schedule`` table introduced in migration b022
(per ADR-0093). Shape mirrors
:class:`~core.repositories.irene_schedule_repository.IreneScheduleRepository`:
a tenant-scoped :class:`AsyncSession`, a frozen DTO, and ``tenant_id``
implicit in the session context (RLS WITH CHECK derives it from
``app.tenant_id`` — the repository never filters on ``tenant_id``).

v0 configures cadence at the tenant level only: exactly one row per
tenant with ``user_id IS NULL``. The cross-tenant due read (the claim on
a superuser engine) deliberately does NOT live here — it belongs to the
tick adapter (``services/investments/live_schedule.py`` / ADR-0093), not
on a tenant-scoped repository. Only the scoped writes (upsert, enqueue,
run-completion) live here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import select, text, update

from core.models.market_data_schedule import MarketDataSchedule
from core.repositories.base import BaseRepository


@dataclass(frozen=True)
class MarketDataScheduleDTO:
    """Plain data-only view of a ``market_data_schedule`` row."""

    id: UUID
    tenant_id: UUID
    user_id: UUID | None
    cadence: str
    preferred_hour: int | None
    timezone: str
    enabled: bool
    next_due_at: datetime
    last_run_at: datetime | None
    event_profile: dict
    created_at: datetime
    updated_at: datetime


def _to_dto(model: MarketDataSchedule) -> MarketDataScheduleDTO:
    return MarketDataScheduleDTO(
        id=model.id,
        tenant_id=model.tenant_id,
        user_id=model.user_id,
        cadence=model.cadence,
        preferred_hour=model.preferred_hour,
        timezone=model.timezone,
        enabled=model.enabled,
        next_due_at=model.next_due_at,
        last_run_at=model.last_run_at,
        event_profile=model.event_profile,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


class MarketDataScheduleRepository(BaseRepository):
    """Read and write the tenant-level market-data schedule."""

    async def get_for_tenant(self) -> MarketDataScheduleDTO | None:
        """Return the v0 tenant-level schedule row (``user_id IS NULL``).

        Returns:
            The tenant-level :class:`MarketDataScheduleDTO`, or ``None`` if
            no schedule has been configured for the active tenant.
        """
        # populate_existing so the re-read at the tail of
        # upsert_tenant_schedule (after an in-transaction Core UPDATE that
        # bypasses the ORM identity map) reflects the freshly written row.
        result = await self._session.execute(
            select(MarketDataSchedule)
            .where(MarketDataSchedule.user_id.is_(None))
            .execution_options(populate_existing=True)
        )
        model = result.scalar_one_or_none()
        return _to_dto(model) if model is not None else None

    async def upsert_tenant_schedule(
        self,
        *,
        cadence: str,
        preferred_hour: int | None,
        timezone: str,
        enabled: bool,
        next_due_at: datetime,
    ) -> MarketDataScheduleDTO:
        """Insert or update the tenant-level schedule (``user_id = NULL``).

        Implemented as read-then-write rather than ``ON CONFLICT``: the
        ``(tenant_id, user_id)`` unique constraint does not match a
        ``user_id IS NULL`` conflict (NULLs are distinct in a Postgres
        unique index), so an ``ON CONFLICT DO UPDATE`` would insert a
        duplicate tenant-level row instead of updating the existing one.
        v0 has a single tenant-level row and the caller holds the
        transaction, so the read-then-write is safe (ADR-0093, mirroring
        ADR-0085).

        Args:
            cadence: The cadence value (v0: ``daily``).
            preferred_hour: Preferred hour of day, or ``None``.
            timezone: The tenant's timezone (IANA name).
            enabled: Whether live import runs for this tenant.
            next_due_at: When the next refresh is due.

        Returns:
            The upserted tenant-level :class:`MarketDataScheduleDTO`.
        """
        existing = await self.get_for_tenant()
        if existing is None:
            tenant_row = await self._session.execute(
                text("SELECT current_setting('app.tenant_id')::uuid AS tid")
            )
            active_tenant: UUID = tenant_row.scalar_one()

            model = MarketDataSchedule(
                tenant_id=active_tenant,
                user_id=None,
                cadence=cadence,
                preferred_hour=preferred_hour,
                timezone=timezone,
                enabled=enabled,
                next_due_at=next_due_at,
            )
            self._session.add(model)
            await self._session.flush()
            await self._session.refresh(model)
            return _to_dto(model)

        await self._session.execute(
            update(MarketDataSchedule)
            .where(MarketDataSchedule.id == existing.id)
            .values(
                cadence=cadence,
                preferred_hour=preferred_hour,
                timezone=timezone,
                enabled=enabled,
                next_due_at=next_due_at,
            )
        )
        await self._session.flush()

        refreshed = await self.get_for_tenant()
        # The row exists: we just updated it in this transaction.
        assert refreshed is not None
        return refreshed

    async def enqueue_due_now(self, *, now: datetime) -> None:
        """Bring the tenant-level schedule due immediately (out-of-cadence).

        Backs the Admin "Refresh now" action (ADR-0093 §"On-demand trigger
        shares the same core"): it does **not** run provider work inline —
        it simply moves the tenant-level row's ``next_due_at`` to ``now`` so
        the next systemd tick claims the tenant and runs a refresh. Unlike
        :meth:`upsert_tenant_schedule`, it overwrites only ``next_due_at``,
        leaving ``cadence`` / ``preferred_hour`` / ``timezone`` / ``enabled``
        untouched. In particular it does **not** flip ``enabled`` — a
        disabled schedule stays disabled and is still skipped by the due
        read (which gates on ``enabled AND next_due_at <= now()``); the
        route only offers this action when the schedule is enabled.

        No-op-safe: the Core UPDATE targets the tenant-level row
        (``user_id IS NULL``) in the active RLS context. If the tenant has
        no schedule row yet it matches zero rows and silently does nothing.

        Args:
            now: The instant to set ``next_due_at`` to (timezone-aware UTC).
        """
        await self._session.execute(
            update(MarketDataSchedule)
            .where(MarketDataSchedule.user_id.is_(None))
            .values(next_due_at=now)
        )
        await self._session.flush()

    async def mark_run_done(
        self,
        *,
        schedule_id: UUID,
        last_run_at: datetime,
        next_due_at: datetime,
    ) -> None:
        """Record that a refresh completed and advance the schedule.

        Written by the tick adapter (ADR-0093) after a tenant's refresh
        runs to completion: it stamps ``last_run_at`` and moves
        ``next_due_at`` forward to the cadence's next occurrence (computed
        by :func:`services.irene.scheduling.compute_next_due_at`, the shared
        cadence arithmetic). Runs **inside** the tenant context — the same
        tenant-scoped session / transaction the refresh wrote its rows on —
        so the write is RLS-policed exactly like the ingest appends. The
        cross-tenant due *read* deliberately lives elsewhere (on the
        superuser connection in ``services/investments/live_schedule.py``);
        only this scoped *write* belongs on the repository.

        Args:
            schedule_id: The ``market_data_schedule`` row to advance.
            last_run_at: When this refresh ran.
            next_due_at: When the next refresh becomes due.
        """
        await self._session.execute(
            update(MarketDataSchedule)
            .where(MarketDataSchedule.id == schedule_id)
            .values(last_run_at=last_run_at, next_due_at=next_due_at)
        )
        await self._session.flush()
