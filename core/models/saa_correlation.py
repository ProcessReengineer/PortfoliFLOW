# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""SAA pairwise-correlation ORM model.

Backs the ``saa_correlations`` table introduced in migration b005.
Correlations are stored upper-triangle only — the b005 CHECK
``asset_class_a_id < asset_class_b_id`` enforces it at the database
level. The diagonal (always 1.0) and the lower triangle (mirror of
the upper triangle) are not stored. The service layer fills both in
when assembling the correlation matrix for the optimiser.

Per ADR-0042 §1, ``tenant_id`` is denormalised so RLS evaluates row-
locally; ``configuration_id`` carries ``ON DELETE CASCADE`` so
deleting a configuration automatically removes its correlation rows.
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


class SAACorrelation(Base):
    """One upper-triangle correlation triplet inside an SAA configuration."""

    __tablename__ = "saa_correlations"
    __table_args__ = (
        CheckConstraint(
            "correlation >= -1 AND correlation <= 1",
            name="ck_saa_correlations_correlation_range",
        ),
        CheckConstraint(
            "asset_class_a_id < asset_class_b_id",
            name="ck_saa_correlations_upper_triangle",
        ),
        UniqueConstraint(
            "configuration_id",
            "asset_class_a_id",
            "asset_class_b_id",
            name="uq_saa_correlations_config_pair",
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
    asset_class_a_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("asset_classes.id", ondelete="RESTRICT"),
        nullable=False,
    )
    asset_class_b_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("asset_classes.id", ondelete="RESTRICT"),
        nullable=False,
    )
    correlation: Mapped[Decimal] = mapped_column(Numeric(8, 6), nullable=False)
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
