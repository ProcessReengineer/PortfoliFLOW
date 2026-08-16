# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Audit-log ORM model.

Every write to a domain table fires an ``AFTER`` trigger that appends a
row here. The trigger is defined in the initial Alembic migration; this
ORM model gives the application read-side typing for the same table.

Per ADR-0035 §7, ``audit_log`` is itself tenant-scoped and RLS-policed:
each tenant sees only its own audit entries.

In Phase 1, ``user_id`` is always NULL because authentication is not
yet wired (Phase 2 / ADR-0036). The schema is shaped so the column
becomes meaningful additively, without a migration.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, String, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from core.models.base import Base


class AuditLog(Base):
    """Append-only audit record for every domain-table write."""

    __tablename__ = "audit_log"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=text("gen_random_uuid()"),
    )
    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="RESTRICT"),
        nullable=False,
    )
    user_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    table_name: Mapped[str] = mapped_column(String(128), nullable=False)
    record_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    operation: Mapped[str] = mapped_column(String(16), nullable=False)
    old_data: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    new_data: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
