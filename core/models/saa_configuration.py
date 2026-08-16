# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""SAA-configuration ORM model — top-level SAA entity per ADR-0042 §1.

Backs the ``saa_configurations`` table introduced in migration b005.
Each row is a named, tenant-scoped Strategic Asset Allocation
configuration carrying the optimisation parameters (risk-free rate,
frontier-point count) and the active-marker flag. The partial unique
index ``uq_saa_configurations_active_per_tenant`` (DB-side, not ORM-
visible) enforces "at most one active configuration per tenant" — a
plain ``UniqueConstraint`` cannot express the ``WHERE is_active = TRUE``
predicate, so the ORM stays silent and the constraint lives in the
migration only. The repository's ``set_active`` operation deactivates
peers in the same transaction so the partial index is never violated.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from core.models.base import Base


class SAAConfiguration(Base):
    """One SAA configuration belonging to exactly one tenant."""

    __tablename__ = "saa_configurations"
    __table_args__ = (
        CheckConstraint(
            "n_frontier_points >= 20 AND n_frontier_points <= 500",
            name="ck_saa_configurations_n_frontier_points_range",
        ),
        UniqueConstraint("tenant_id", "name", name="uq_saa_configurations_tenant_name"),
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
    name: Mapped[str] = mapped_column(Text, nullable=False)
    risk_free_rate: Mapped[Decimal] = mapped_column(
        Numeric(8, 6),
        nullable=False,
        server_default=text("0.0"),
    )
    n_frontier_points: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("100"),
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("FALSE"),
    )
    created_by: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
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
    )
