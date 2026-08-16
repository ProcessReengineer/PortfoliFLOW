# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""MarketDataSchedule ORM model — live-import cadence configuration.

Backs the ``market_data_schedule`` table introduced in migration b022
(per ADR-0093). It is the market-data analogue of
:class:`~core.models.irene_schedule.IreneSchedule`: a per-tenant (later
per-user) cadence config owners edit in the Admin surface, deliberately
kept in the database rather than a systemd/cron unit because cadence is
legitimate per-tenant calibration (ADR-0093 §"Per-tenant cadence in a
config table"), not fixed infrastructure.

``enabled`` defaults to **FALSE** (contrast :class:`IreneSchedule`, which
defaults TRUE): a freshly provisioned tenant does not silently start
fetching from external providers. The seed row installed for every tenant
(ADR-0077 parity, through the ``seed_tenant_defaults`` choke-point /
bootstrap) therefore lands disabled; an owner opts the tenant in.

``user_id`` is nullable and present from day one to draw the per-user
seam without a later schema change, but is **unused in v0**: v0 configures
cadence at the tenant level only (exactly one row per tenant with
``user_id IS NULL``).

Caveat (deliberate, mirroring ADR-0085): the ``(tenant_id, user_id)``
unique constraint does not prevent duplicate ``(tenant_id, NULL)`` rows,
because NULLs are distinct in a Postgres unique index. The single tenant-
level row is upserted through
:class:`~core.repositories.market_data_schedule_repository.MarketDataScheduleRepository`,
which reads-then-writes rather than relying on ``ON CONFLICT``.

``event_profile`` is reserved for per-tenant event-trigger selection (v1,
ADR-0093 §Consequences); it is empty and unused in v0, so the event seam
needs no later migration. ``last_run_at`` records the last successful
refresh — the market-data counterpart of ``irene_schedule.last_beat_at``.
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


class MarketDataSchedule(Base):
    """Per-tenant live-import cadence configuration (per-user seam reserved)."""

    __tablename__ = "market_data_schedule"
    __table_args__ = (
        # NOTE: NULLs are distinct in a Postgres unique index, so this does
        # NOT enforce a single (tenant_id, NULL) row. Tenant-level
        # uniqueness is upheld by the repository's read-then-write upsert in
        # v0 (ADR-0093); the per-user seam is left loose deliberately.
        UniqueConstraint(
            "tenant_id",
            "user_id",
            name="uq_market_data_schedule_tenant_user",
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
    # Defaults FALSE (ADR-0093): a fresh tenant does not silently fetch.
    enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
    )
    next_due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
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
