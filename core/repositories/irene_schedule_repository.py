# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""IreneScheduleRepository — persistence for Irene's cadence configuration.

Backs the ``irene_schedule`` table introduced in migration b019 (per
ADR-0085 §``irene_schedule``). Shape mirrors the other tenant-scoped
repositories: tenant-scoped :class:`AsyncSession`, a frozen DTO,
``tenant_id`` implicit in the session context (RLS WITH CHECK derives
it from ``app.tenant_id``).

v0 configures cadence at the tenant level only: exactly one row per
tenant with ``user_id IS NULL``. The due-evaluation query (the
cross-tenant claim on a superuser engine) deliberately does NOT live
here — it belongs to the tick adapter (Prompt 2 / ADR-0086), not on a
tenant-scoped repository.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import select, text, update

from core.models.irene_schedule import IreneSchedule
from core.repositories.base import BaseRepository


@dataclass(frozen=True)
class IreneScheduleDTO:
    """Plain data-only view of an ``irene_schedule`` row."""

    id: UUID
    tenant_id: UUID
    user_id: UUID | None
    cadence: str
    preferred_hour: int | None
    timezone: str
    enabled: bool
    next_due_at: datetime
    last_beat_at: datetime | None
    event_profile: dict
    created_at: datetime
    updated_at: datetime


def _to_dto(model: IreneSchedule) -> IreneScheduleDTO:
    return IreneScheduleDTO(
        id=model.id,
        tenant_id=model.tenant_id,
        user_id=model.user_id,
        cadence=model.cadence,
        preferred_hour=model.preferred_hour,
        timezone=model.timezone,
        enabled=model.enabled,
        next_due_at=model.next_due_at,
        last_beat_at=model.last_beat_at,
        event_profile=model.event_profile,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


class IreneScheduleRepository(BaseRepository):
    """Read and write the tenant-level Irene schedule."""

    async def get_for_tenant(self) -> IreneScheduleDTO | None:
        """Return the v0 tenant-level schedule row (``user_id IS NULL``).

        Returns:
            The tenant-level :class:`IreneScheduleDTO`, or ``None`` if no
            schedule has been configured for the active tenant.
        """
        # populate_existing so the re-read at the tail of
        # upsert_tenant_schedule (after an in-transaction Core UPDATE that
        # bypasses the ORM identity map) reflects the freshly written row.
        result = await self._session.execute(
            select(IreneSchedule)
            .where(IreneSchedule.user_id.is_(None))
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
    ) -> IreneScheduleDTO:
        """Insert or update the tenant-level schedule (``user_id = NULL``).

        Implemented as read-then-write rather than ``ON CONFLICT``: the
        ``(tenant_id, user_id)`` unique constraint does not match a
        ``user_id IS NULL`` conflict (NULLs are distinct in a Postgres
        unique index), so an ``ON CONFLICT DO UPDATE`` would insert a
        duplicate tenant-level row instead of updating the existing one.
        v0 has a single tenant-level row and the caller holds the
        transaction, so the read-then-write is safe (ADR-0085).

        Args:
            cadence: The cadence value, validated by the caller through
                ``compute_next_due_at`` (ADR-0119 §1).
            preferred_hour: Preferred hour of day, or ``None``.
            timezone: The tenant's timezone (IANA name).
            enabled: Whether Irene runs for this tenant.
            next_due_at: When the next beat is due.

        Returns:
            The upserted tenant-level :class:`IreneScheduleDTO`.
        """
        existing = await self.get_for_tenant()
        if existing is None:
            tenant_row = await self._session.execute(
                text("SELECT current_setting('app.tenant_id')::uuid AS tid")
            )
            active_tenant: UUID = tenant_row.scalar_one()

            model = IreneSchedule(
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
            update(IreneSchedule)
            .where(IreneSchedule.id == existing.id)
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

        Backs the Watch Desk "Request analysis" action (ADR-0089):
        it does **not** run synthesis inline (that would break the
        heartbeat model of ADR-0086) — it simply moves the tenant-level
        row's ``next_due_at`` to ``now`` so the next systemd tick claims
        the tenant and runs a beat. Unlike
        :meth:`upsert_tenant_schedule`, it overwrites only ``next_due_at``,
        leaving ``cadence`` / ``preferred_hour`` / ``timezone`` / ``enabled``
        untouched.

        No-op-safe: the Core UPDATE targets the tenant-level row
        (``user_id IS NULL``) in the active RLS context. If the tenant has
        no schedule row yet it matches zero rows and silently does nothing
        — the caller (the Watch Desk route) hides the button in that case
        rather than relying on this to error.

        Args:
            now: The instant to set ``next_due_at`` to (timezone-aware UTC).
        """
        await self._session.execute(
            update(IreneSchedule).where(IreneSchedule.user_id.is_(None)).values(next_due_at=now)
        )
        await self._session.flush()

    async def mark_beat_done(
        self,
        *,
        schedule_id: UUID,
        last_beat_at: datetime,
        next_due_at: datetime,
    ) -> None:
        """Record that a beat completed and advance the schedule.

        Written by the tick adapter (ADR-0086) after a tenant's beat runs
        to completion: it stamps ``last_beat_at`` and moves ``next_due_at``
        forward to the cadence's next occurrence (computed by
        :func:`services.irene.scheduling.compute_next_due_at`). Runs
        **inside** the tenant context — the same tenant-scoped session /
        transaction the beat wrote its findings on — so the write is
        RLS-policed exactly like the beat's ``irene_finding`` appends.
        The cross-tenant due *read* deliberately lives elsewhere (on the
        superuser connection in ``services/irene/scheduling.py``); only
        this scoped *write* belongs on the repository.

        Args:
            schedule_id: The ``irene_schedule`` row to advance.
            last_beat_at: When this beat ran.
            next_due_at: When the next beat becomes due.
        """
        await self._session.execute(
            update(IreneSchedule)
            .where(IreneSchedule.id == schedule_id)
            .values(last_beat_at=last_beat_at, next_due_at=next_due_at)
        )
        await self._session.flush()
