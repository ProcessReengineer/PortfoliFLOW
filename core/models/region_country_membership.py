# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""RegionCountryMembership ORM model — region/country mapping.

Backs the ``region_country_memberships`` table introduced in
migration b009 (per ADR-0046). Maps each ISO 3166-1 alpha-2 country
to exactly one region per tenant; the strict-partition invariant is
enforced by a UNIQUE constraint on ``(tenant_id, country_iso_code)``.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CHAR,
    DateTime,
    ForeignKey,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from core.models.base import Base


class RegionCountryMembership(Base):
    """One ``(region, country)`` membership row in the active tenant."""

    __tablename__ = "region_country_memberships"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "country_iso_code",
            name="uq_region_country_memberships_tenant_iso_unique",
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
    region_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("regions.id", ondelete="CASCADE"),
        nullable=False,
    )
    country_iso_code: Mapped[str] = mapped_column(
        CHAR(2),
        ForeignKey("countries.iso_code", ondelete="RESTRICT"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
