# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Per-asset-class SAA input ORM model.

Backs the ``saa_asset_class_inputs`` table introduced in migration
b005. Each row carries the forward-looking expectation (expected
return, volatility) and weight bounds (min, max) for one asset class
inside one SAA configuration. ``tenant_id`` is denormalised from
``saa_configurations`` per ADR-0035 §3 so RLS evaluates row-locally
without a JOIN.

Range and ordering invariants (volatility ≥ 0, 0 ≤ min_weight ≤ 1,
0 ≤ max_weight ≤ 1, min_weight ≤ max_weight) live as DB-side CHECK
constraints in b005. The service layer performs the same checks
in-process before any write so a validation error surfaces to the
caller as ``SAAValidationError`` rather than as a Postgres-level
``IntegrityError``.
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


class SAAAssetClassInput(Base):
    """One per-asset-class row inside an SAA configuration."""

    __tablename__ = "saa_asset_class_inputs"
    __table_args__ = (
        CheckConstraint(
            "volatility >= 0",
            name="ck_saa_asset_class_inputs_volatility_nonneg",
        ),
        CheckConstraint(
            "min_weight >= 0 AND min_weight <= 1",
            name="ck_saa_asset_class_inputs_min_weight_range",
        ),
        CheckConstraint(
            "max_weight >= 0 AND max_weight <= 1",
            name="ck_saa_asset_class_inputs_max_weight_range",
        ),
        CheckConstraint(
            "min_weight <= max_weight",
            name="ck_saa_asset_class_inputs_min_le_max",
        ),
        UniqueConstraint(
            "configuration_id",
            "asset_class_id",
            name="uq_saa_asset_class_inputs_config_asset",
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
    configuration_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("saa_configurations.id", ondelete="CASCADE"),
        nullable=False,
    )
    asset_class_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("asset_classes.id", ondelete="RESTRICT"),
        nullable=False,
    )
    expected_return: Mapped[Decimal] = mapped_column(Numeric(8, 6), nullable=False)
    volatility: Mapped[Decimal] = mapped_column(Numeric(8, 6), nullable=False)
    min_weight: Mapped[Decimal] = mapped_column(
        Numeric(8, 6),
        nullable=False,
        server_default=text("0.0"),
    )
    max_weight: Mapped[Decimal] = mapped_column(
        Numeric(8, 6),
        nullable=False,
        server_default=text("1.0"),
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
