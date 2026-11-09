# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""The one way a test writes a legacy record row (D-CS-B-91).

``note``, ``pin`` and ``decision_record`` are retired for new writes
(ADR-0138 §9): no route writes them since P-CS-4a, and the repository's
generic entry writer that refused them was retired with P-CS-H1. Legacy rows
still exist in tenant data (D-CS-M-7, no data migration) and the Record still
renders them, so the tests that prove that seed such rows here — a
model-level ``CaseEntry`` insert under the caller's tenant context,
RLS-policed like any app-role write, with no vocabulary or state gate.
Nothing in production imports this module.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession

from core.models.case import CaseEntry


async def seed_record_entry(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    case_id: UUID,
    kind: str,
    actor: str,
    actor_user_id: UUID | None,
    payload: dict[str, Any],
    now: datetime,
) -> UUID:
    """Insert one ``case_entries`` row as a legacy writer would have, and return its id.

    Args:
        session: A tenant-scoped session (``tenant_context`` of the case's tenant).
        tenant_id: The case's tenant (``case_entries.tenant_id`` is denormalised).
        case_id: The case.
        kind: The entry kind — typically ``note``, ``pin`` or ``decision_record``.
        actor: ``pm`` · ``shirley`` · ``system``.
        actor_user_id: The acting user, or ``None``.
        payload: The opaque payload.
        now: The entry's ``created_at``.

    Returns:
        The new entry's id.
    """
    result = await session.execute(
        insert(CaseEntry)
        .values(
            tenant_id=tenant_id,
            case_id=case_id,
            kind=kind,
            actor=actor,
            actor_user_id=actor_user_id,
            payload=payload,
            created_at=now,
        )
        .returning(CaseEntry.id)
    )
    return result.scalar_one()
