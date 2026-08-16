# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""IreneScheduleRepository tests against the live compose Postgres.

The ``irene_schedule`` table is tenant-scoped (RLS-policed, per ADR-0085
/ ADR-0035). v0 stores exactly one tenant-level row per tenant
(``user_id IS NULL``). ``upsert_tenant_schedule`` is a read-then-write
upsert (an ``ON CONFLICT`` would never match a NULL ``user_id``), so a
second call must update the existing row rather than insert a duplicate.

Coverage
--------
* ISC-01: ``upsert_tenant_schedule`` roundtrip; ``get_for_tenant`` reads
  the row back and the DTO fields survive.
* ISC-02: a second ``upsert_tenant_schedule`` updates the tenant-level
  row in place (one row, ``user_id IS NULL``).
* ISC-03: a schedule written under tenant A is invisible from a session
  under tenant B (RLS smoke).
* ISC-04: ``mark_beat_done`` advances ``last_beat_at`` / ``next_due_at``
  in place, leaving the other fields untouched (ADR-0086 beat-completion
  write).
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from core.repositories import tenant_context
from core.repositories.irene_schedule_repository import IreneScheduleRepository


# ---------------------------------------------------------------------------
# ISC-01: upsert roundtrip
# ---------------------------------------------------------------------------


async def test_isc01_upsert_tenant_schedule_roundtrip(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant("ISC-01")
    due = datetime(2026, 7, 3, 6, 0, tzinfo=timezone.utc)

    async with tenant_context(app_engine, tenant_id) as session:
        repo = IreneScheduleRepository(session)
        # No schedule configured yet.
        assert await repo.get_for_tenant() is None

        created = await repo.upsert_tenant_schedule(
            cadence="daily",
            preferred_hour=6,
            timezone="Europe/Berlin",
            enabled=True,
            next_due_at=due,
        )
        assert created.user_id is None
        assert created.cadence == "daily"
        assert created.preferred_hour == 6
        assert created.timezone == "Europe/Berlin"
        assert created.enabled is True
        assert created.next_due_at == due
        # event_profile defaults to an empty object (reserved, v1).
        assert created.event_profile == {}

        fetched = await repo.get_for_tenant()
        assert fetched is not None
        assert fetched.id == created.id
        assert fetched.cadence == "daily"


# ---------------------------------------------------------------------------
# ISC-02: second upsert updates the tenant-level row in place
# ---------------------------------------------------------------------------


async def test_isc02_upsert_updates_in_place(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant("ISC-02")
    due0 = datetime(2026, 7, 3, 6, 0, tzinfo=timezone.utc)
    due1 = datetime(2026, 7, 4, 5, 0, tzinfo=timezone.utc)

    async with tenant_context(app_engine, tenant_id) as session:
        repo = IreneScheduleRepository(session)
        first = await repo.upsert_tenant_schedule(
            cadence="daily",
            preferred_hour=6,
            timezone="Europe/Berlin",
            enabled=True,
            next_due_at=due0,
        )
        second = await repo.upsert_tenant_schedule(
            cadence="daily",
            preferred_hour=5,
            timezone="Europe/Berlin",
            enabled=False,
            next_due_at=due1,
        )

        # Same row updated, not a duplicate NULL-user_id row.
        assert second.id == first.id
        assert second.preferred_hour == 5
        assert second.enabled is False
        assert second.next_due_at == due1

        count = await session.execute(
            text("SELECT count(*) FROM irene_schedule WHERE user_id IS NULL")
        )
        assert count.scalar_one() == 1


# ---------------------------------------------------------------------------
# ISC-03: cross-tenant invisibility (RLS smoke)
# ---------------------------------------------------------------------------


async def test_isc03_cross_tenant_invisibility(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_a = await seed_tenant(name="A")
    tenant_b = await seed_tenant(name="B")
    due = datetime(2026, 7, 3, 6, 0, tzinfo=timezone.utc)

    async with tenant_context(app_engine, tenant_a) as session:
        await IreneScheduleRepository(session).upsert_tenant_schedule(
            cadence="daily",
            preferred_hour=6,
            timezone="Europe/Berlin",
            enabled=True,
            next_due_at=due,
        )

    async with tenant_context(app_engine, tenant_b) as session:
        repo = IreneScheduleRepository(session)
        assert await repo.get_for_tenant() is None
        count = await session.execute(text("SELECT count(*) FROM irene_schedule"))
        assert count.scalar_one() == 0


# ---------------------------------------------------------------------------
# ISC-04: mark_beat_done advances the schedule in place
# ---------------------------------------------------------------------------


async def test_isc04_mark_beat_done_advances_schedule(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant("ISC-04")
    due0 = datetime(2026, 7, 3, 6, 0, tzinfo=timezone.utc)
    beat_at = datetime(2026, 7, 3, 6, 0, 5, tzinfo=timezone.utc)
    next_due = datetime(2026, 7, 4, 6, 0, tzinfo=timezone.utc)

    async with tenant_context(app_engine, tenant_id) as session:
        repo = IreneScheduleRepository(session)
        created = await repo.upsert_tenant_schedule(
            cadence="daily",
            preferred_hour=6,
            timezone="Europe/Berlin",
            enabled=True,
            next_due_at=due0,
        )
        assert created.last_beat_at is None

        await repo.mark_beat_done(
            schedule_id=created.id,
            last_beat_at=beat_at,
            next_due_at=next_due,
        )

        advanced = await repo.get_for_tenant()
        assert advanced is not None
        # Same row, advanced in place.
        assert advanced.id == created.id
        assert advanced.last_beat_at == beat_at
        assert advanced.next_due_at == next_due
        # Untouched fields survive the beat-completion write.
        assert advanced.cadence == "daily"
        assert advanced.preferred_hour == 6
        assert advanced.enabled is True
