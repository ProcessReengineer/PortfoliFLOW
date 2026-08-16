# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""IreneSchedule ORM model — Watch Desk cadence configuration.

Backs the ``irene_schedule`` table introduced in migration b019 (per
ADR-0085 §``irene_schedule``). Cadence is a per-tenant (later per-user)
domain concern owners edit in settings — the calibration interface of
the heartbeat (ADR-0086), not a fixed infrastructure tick — so it lives
in the database rather than a systemd/cron unit.

``user_id`` is nullable and present from day one to draw the per-user
seam without a later schema change, but is **unused in v0**: v0
configures cadence at the tenant level only, i.e. exactly one row per
tenant with ``user_id IS NULL``.

Caveat (deliberate, per ADR-0085): the ``(tenant_id, user_id)`` unique
constraint does not prevent duplicate ``(tenant_id, NULL)`` rows,
because in Postgres NULLs are distinct in a unique index. That is
acceptable for v0 — the single tenant-level row is upserted through
:class:`~core.repositories.irene_schedule_repository.IreneScheduleRepository`,
which reads-then-writes rather than relying on ``ON CONFLICT`` (which
would never match a NULL ``user_id``). No partial unique index or
coalesce trick is added here; the per-user seam is intentionally left
loose.

``event_profile`` is reserved for per-tenant event-trigger selection
(v1); it is empty and unused in v0, so the event seam needs no later
migration.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    SmallInteger,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from core.models.base import Base


class IreneSchedule(Base):
    """Per-tenant Irene cadence configuration (per-user seam reserved)."""

    __tablename__ = "irene_schedule"
    __table_args__ = (
        # NOTE: NULLs are distinct in a Postgres unique index, so this
        # does NOT enforce a single (tenant_id, NULL) row. The tenant-level
        # uniqueness is upheld by the repository's read-then-write upsert
        # in v0 (ADR-0085); the per-user seam is left loose deliberately.
        UniqueConstraint(
            "tenant_id",
            "user_id",
            name="uq_irene_schedule_tenant_user",
        ),
    )

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
    # Per-user seam: nullable and present from day one, unused in v0.
    user_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    cadence: Mapped[str] = mapped_column(Text, nullable=False)
    preferred_hour: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    timezone: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("true"),
    )
    next_due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_beat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Reserved for per-tenant event-trigger selection (v1); empty in v0.
    event_profile: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
