# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""The case wake-up's tenant enumeration (ADR-0138 §4, ADR-0117 §3, P-CS-2c).

The wake-up is the third step of the shared tick runner
(:func:`services.scheduler.tick_runner.run_case_wake_tick`): on the first
tick of the day a waiting case's ``wait_until`` is reached, the case
returns to open. Like the other two domains, the runner first asks a
cross-tenant question on its RLS-bypassing engine and then works tenant by
tenant inside ``tenant_context``. This module holds that question.

**It reads ``irene_schedule`` and nothing else** (D-CS-B-26). ADR-0117 §3
confines the runner's superuser-privileged surface to the two schedule
tables. Every tenant carries a seeded Irene schedule row
(``install_irene_schedule`` — at bootstrap and at super-admin tenant
creation), so the distinct tenant ids of that table enumerate the tick
domain's tenants without the privileged connection ever touching a case.
Whether a tenant *has* a due case is asked inside its own
``tenant_context``, RLS enforced
(:meth:`core.repositories.case_repository.CaseRepository.list_due_for_wake`)
— one cheap read per tenant per tick. Neither ``enabled`` nor
``next_due_at`` matters here: a wake-up needs no beat, only a tenant.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


async def find_case_tick_tenants(conn: AsyncConnection) -> list[UUID]:
    """Return every tenant that has an Irene schedule row, id ascending.

    Runs on a **superuser** connection: RLS is bypassed intentionally, as
    in :func:`services.irene.scheduling.find_due_tenants` — a platform-level
    read spanning all tenants, before any tenant context exists. The
    statement names the schedule table only (ADR-0117 §3, D-CS-B-26).

    Args:
        conn: A superuser :class:`AsyncConnection` (RLS-bypassing).

    Returns:
        The distinct tenant ids, ascending.
    """
    result = await conn.execute(
        text("SELECT DISTINCT tenant_id FROM irene_schedule ORDER BY tenant_id")
    )
    return [UUID(str(row.tenant_id)) for row in result]


__all__ = ["find_case_tick_tenants"]
