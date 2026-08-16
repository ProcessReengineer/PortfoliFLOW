# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Limit ORM model — one (limit_set, class_key) ceiling.

Backs the ``limits`` table introduced in migration b010 (per
ADR-0056 §Schema). Per-class maximum-share row belonging to exactly
one ``limit_set``. ``class_key`` is a string snapshot, **not** a
foreign key — resolution against ``asset_classes.code`` (for
``family = 'saa'``) or ``anlv_categories.code`` (for
``family = 'anlv'``) is performed family-polymorphically in the
importer at write time, per ADR-0056 §Decision.

``tenant_id`` is denormalised from ``limit_sets.tenant_id`` so RLS
evaluates row-locally per ADR-0035 §3. The
:class:`LimitsRepository.create_set_with_limits` writer keeps the
two columns consistent by sourcing both from the active
``app.tenant_id`` setting.
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
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from core.models.base import Base


class Limit(Base):
    """One (limit_set, class_key) ceiling row."""

    __tablename__ = "limits"
    __table_args__ = (
        CheckConstraint(
            "max_pct > 0 AND max_pct <= 100",
            name="ck_limits_max_pct_range",
        ),
        UniqueConstraint(
            "limit_set_id",
            "class_key",
            name="uq_limits_set_class_key",
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
    limit_set_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("limit_sets.id", ondelete="RESTRICT"),
        nullable=False,
    )
    class_key: Mapped[str] = mapped_column(Text, nullable=False)
    max_pct: Mapped[Decimal] = mapped_column(Numeric(7, 4), nullable=False)
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
