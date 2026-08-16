# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Cross-tenant due evaluation for the market-data live-import tick.

Per ADR-0093 the *act* of ticking is dumb, tenant-blind infrastructure
(a systemd timer); the interesting question — "which tenants are due a
refresh now" — lives here in the domain layer, mirroring Irene's
``services/irene/scheduling.find_due_tenants`` (ADR-0086) exactly.

:func:`find_due_tenants` is the **cross-tenant** due read. It runs on a
superuser connection with RLS intentionally bypassed (a platform-level
scheduler read spanning all tenants, run *before* entering any single
tenant's context), so it does NOT live on the tenant-scoped
:class:`~core.repositories.market_data_schedule_repository.MarketDataScheduleRepository`.
The scoped run-completion *write* (``mark_run_done``) does live on that
repository.

The two other scheduling concerns are shared with Irene and reused
rather than duplicated (both are pure, domain-neutral):

- Cadence arithmetic — :func:`services.irene.scheduling.compute_next_due_at`
  (given a clock / cadence / preferred hour / timezone, the next UTC
  instant; v0 supports ``daily``). The market-data schedule uses the same
  ``daily`` vocabulary, so the DST-aware zoneinfo logic is not re-written.
- Advisory-lock keys — :func:`services.irene.scheduling.advisory_lock_key`,
  called with ``domain="market_data"`` so a market-data claim never
  collides with an Irene beat's lock (ADR-0093 §0.2).

This module imports only stdlib and SQLAlchemy; it performs no writes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


@dataclass(frozen=True)
class DueMarketDataTenant:
    """One tenant whose market-data refresh is due, from the cross-tenant read.

    Attributes:
        tenant_id: The tenant to refresh (scoping key for the refresh's
            ``tenant_context``).
        schedule_id: The ``market_data_schedule`` row id, so the tick can
            call ``mark_run_done`` without re-reading the schedule.
        cadence: The schedule's cadence (v0: ``daily``).
        timezone: The tenant's IANA timezone name (e.g. ``Europe/Berlin``),
            used to place ``preferred_hour``.
        preferred_hour: The preferred hour of day (0–23), or ``None``.
        last_run_at: When this tenant last refreshed successfully, or
            ``None`` if it never has — drives the fetch-window lower bound
            in the refresh core.
    """

    tenant_id: UUID
    # ``None`` only on the ``--tenant`` test-seam path for a tenant that has
    # no schedule row yet; the cross-tenant due read always sets it.
    schedule_id: UUID | None
    cadence: str
    timezone: str
    preferred_hour: int | None
    last_run_at: datetime | None


async def find_due_tenants(conn: AsyncConnection) -> list[DueMarketDataTenant]:
    """Return every enabled tenant whose next refresh is due, DB-clock based.

    Runs ``SELECT ... WHERE enabled AND next_due_at <= now()`` on a
    **superuser** connection: RLS is bypassed intentionally because this is
    a platform-level scheduler read spanning all tenants, run before any
    tenant context exists (ADR-0093, mirroring ADR-0086). ``now()`` is
    evaluated DB-side, so every tenant is compared against one clock.

    Args:
        conn: A superuser :class:`AsyncConnection` (RLS-bypassing).

    Returns:
        A list of :class:`DueMarketDataTenant`, one per due schedule row.
        Empty when nothing is due — the common, near-free case.
    """
    result = await conn.execute(
        text(
            "SELECT tenant_id, id AS schedule_id, cadence, timezone, "
            "preferred_hour, last_run_at "
            "FROM market_data_schedule "
            "WHERE enabled AND next_due_at <= now()"
        )
    )
    return [
        DueMarketDataTenant(
            tenant_id=row.tenant_id,
            schedule_id=row.schedule_id,
            cadence=row.cadence,
            timezone=row.timezone,
            preferred_hour=row.preferred_hour,
            last_run_at=row.last_run_at,
        )
        for row in result
    ]


__all__ = ["DueMarketDataTenant", "find_due_tenants"]
