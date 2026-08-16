# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""IreneWatchStateRepository tests against the live compose Postgres.

The ``irene_watch_state`` table is tenant-scoped (RLS-policed, per
ADR-0085 / ADR-0035). One row per monitored subject is upserted per
beat; the ``acknowledged_*`` fields must survive an upsert so the delta
logic (ADR-0086) can diff against them.

Coverage
--------
* IWS-01: ``upsert`` roundtrip; ``get_by_subject`` reads the row back
  and the DTO fields survive.
* IWS-02: two upserts on the same ``subject_key`` update in place (one
  row) and the second does NOT clobber a previously written
  ``acknowledged_magnitude``.
* IWS-03: a row written under tenant A is invisible from a session
  under tenant B (RLS smoke).
* IWS-04: ``list_all`` returns every row in the active tenant context,
  ordered by ``subject_key`` and tenant-scoped (backs the Watch Desk
  monitor's single bulk read, ADR-0089).
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from core.repositories import tenant_context
from core.repositories.irene_watch_state_repository import (
    IreneWatchStateRepository,
)


# ---------------------------------------------------------------------------
# IWS-01: upsert roundtrip
# ---------------------------------------------------------------------------


async def test_iws01_upsert_roundtrip(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant("IWS-01")
    seen = datetime(2026, 7, 1, 6, 0, tzinfo=timezone.utc)

    async with tenant_context(app_engine, tenant_id) as session:
        repo = IreneWatchStateRepository(session)
        created = await repo.upsert(
            subject_key="anlv:16",
            magnitude=Decimal("50.5"),
            band="noteworthy",
            last_seen_at=seen,
        )
        assert created.subject_key == "anlv:16"
        assert created.magnitude == Decimal("50.5")
        assert created.band == "noteworthy"
        assert created.last_seen_at == seen
        # A fresh subject has no acknowledged state.
        assert created.acknowledged_at is None
        assert created.acknowledged_magnitude is None

        fetched = await repo.get_by_subject("anlv:16")
        assert fetched is not None
        assert fetched.id == created.id
        assert fetched.magnitude == Decimal("50.5")
        assert fetched.band == "noteworthy"

        # An unknown subject reads back as None.
        assert await repo.get_by_subject("anlv:99") is None


# ---------------------------------------------------------------------------
# IWS-02: idempotent upsert preserves acknowledged_*
# ---------------------------------------------------------------------------


async def test_iws02_upsert_idempotent_and_preserves_acknowledgement(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant("IWS-02")
    t0 = datetime(2026, 7, 1, 6, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 7, 2, 6, 0, tzinfo=timezone.utc)

    async with tenant_context(app_engine, tenant_id) as session:
        repo = IreneWatchStateRepository(session)
        await repo.upsert(
            subject_key="saa:equity",
            magnitude=Decimal("50.5"),
            band="noteworthy",
            last_seen_at=t0,
        )
        # The user acknowledges the state at 50.5.
        await repo.acknowledge(
            subject_key="saa:equity",
            acknowledged_at=t0,
            acknowledged_magnitude=Decimal("50.5"),
        )
        # Next beat: magnitude escalates, band changes.
        updated = await repo.upsert(
            subject_key="saa:equity",
            magnitude=Decimal("58.0"),
            band="critical",
            last_seen_at=t1,
        )

        # Same row updated in place, not duplicated.
        count = await session.execute(text("SELECT count(*) FROM irene_watch_state"))
        assert count.scalar_one() == 1

        # Beat fields moved; acknowledged fields preserved by the upsert.
        assert updated.magnitude == Decimal("58.0")
        assert updated.band == "critical"
        assert updated.last_seen_at == t1
        assert updated.acknowledged_at == t0
        assert updated.acknowledged_magnitude == Decimal("50.5")

        # reset_acknowledgement nulls the acknowledged_* pair (falling edge).
        await repo.reset_acknowledgement("saa:equity")
        after_reset = await repo.get_by_subject("saa:equity")
        assert after_reset is not None
        assert after_reset.acknowledged_at is None
        assert after_reset.acknowledged_magnitude is None
        # The beat fields are untouched by the reset.
        assert after_reset.magnitude == Decimal("58.0")


# ---------------------------------------------------------------------------
# IWS-03: cross-tenant invisibility (RLS smoke)
# ---------------------------------------------------------------------------


async def test_iws03_cross_tenant_invisibility(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_a = await seed_tenant(name="A")
    tenant_b = await seed_tenant(name="B")
    seen = datetime(2026, 7, 1, 6, 0, tzinfo=timezone.utc)

    async with tenant_context(app_engine, tenant_a) as session:
        await IreneWatchStateRepository(session).upsert(
            subject_key="anlv:16",
            magnitude=Decimal("50.5"),
            band="noteworthy",
            last_seen_at=seen,
        )

    async with tenant_context(app_engine, tenant_b) as session:
        repo = IreneWatchStateRepository(session)
        # RLS hides tenant A's row from tenant B.
        assert await repo.get_by_subject("anlv:16") is None
        count = await session.execute(text("SELECT count(*) FROM irene_watch_state"))
        assert count.scalar_one() == 0


# ---------------------------------------------------------------------------
# IWS-04: list_all — bulk read, ordered and tenant-scoped
# ---------------------------------------------------------------------------


async def test_iws04_list_all_is_ordered_and_tenant_scoped(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_a = await seed_tenant(name="A")
    tenant_b = await seed_tenant(name="B")
    seen = datetime(2026, 7, 1, 6, 0, tzinfo=timezone.utc)

    async with tenant_context(app_engine, tenant_a) as session:
        repo = IreneWatchStateRepository(session)
        # Written out of order — list_all sorts them for a stable render.
        for subject_key in ("saa:equity", "anlv:16", "saa:cash"):
            await repo.upsert(
                subject_key=subject_key,
                magnitude=Decimal("12.5"),
                band="informational",
                last_seen_at=seen,
            )

    async with tenant_context(app_engine, tenant_b) as session:
        await IreneWatchStateRepository(session).upsert(
            subject_key="anlv:17",
            magnitude=Decimal("99.0"),
            band="critical",
            last_seen_at=seen,
        )

    async with tenant_context(app_engine, tenant_a) as session:
        rows = await IreneWatchStateRepository(session).list_all()
        # Tenant B's subject is invisible; tenant A's are sorted.
        assert [row.subject_key for row in rows] == [
            "anlv:16",
            "saa:cash",
            "saa:equity",
        ]
        assert all(row.tenant_id == tenant_a for row in rows)

    # A tenant with no watch state reads back empty, never raising.
    tenant_c = await seed_tenant(name="C")
    async with tenant_context(app_engine, tenant_c) as session:
        assert await IreneWatchStateRepository(session).list_all() == []
