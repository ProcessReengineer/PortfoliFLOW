# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""AssetClassBenchmarkMapping ORM model — asset-class → benchmark mapping with weights.

Backs the ``asset_class_benchmark_mapping`` table introduced in
migration b011 (per ADR-0061 §Decision). Each row is one
``(asset_class, benchmark, weight)`` entry. In Phase 1 each asset
class has at most one mapping row with ``weight = 1.0``;
composite-benchmark support (multiple rows per asset class with
weights summing to ≤ 1) is schema-ready but not exercised yet.

``tenant_id`` is denormalised from ``asset_classes.tenant_id`` per
ADR-0035 §3. ``CHECK (weight >= 0 AND weight <= 1)`` enforces the
bounded weight invariant; the importer raises a hard error before
the row ever reaches the DB so the constraint acts as a
defence-in-depth safety net rather than the primary guard.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Numeric,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from core.models.base import Base


class AssetClassBenchmarkMapping(Base):
    """One ``(asset_class, benchmark, weight)`` mapping row."""

    __tablename__ = "asset_class_benchmark_mapping"
    __table_args__ = (
        CheckConstraint(
            "weight >= 0 AND weight <= 1",
            name="ck_acbm_weight_range",
        ),
        UniqueConstraint(
            "asset_class_id",
            "benchmark_id",
            name="uq_acbm_asset_class_benchmark",
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
    asset_class_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("asset_classes.id", ondelete="CASCADE"),
        nullable=False,
    )
    benchmark_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("benchmarks.id", ondelete="RESTRICT"),
        nullable=False,
    )
    weight: Mapped[Decimal] = mapped_column(Numeric(7, 4), nullable=False)
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
