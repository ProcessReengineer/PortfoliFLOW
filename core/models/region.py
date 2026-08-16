# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Region ORM model — per-tenant aggregation layer over ISO countries.

Backs the ``regions`` table introduced in migration b009 (per
ADR-0046). Each tenant carries a catalogue of regions; a region is a
disjoint group of ISO 3166-1 alpha-2 countries (strict partition: a
country belongs to at most one region per tenant). The Excel
import path resolves Excel region labels (``"DACH"``, ``"Asia
Emerging"``, …) against ``display_name`` to populate
``investment_region_weights``.

Distinct from the free-text ``investment.region`` field, which
describes a single investment's strategy geography and is not part of
the aggregation layer.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from core.models.base import Base


class Region(Base):
    """One region definition belonging to exactly one tenant."""

    __tablename__ = "regions"
    __table_args__ = (UniqueConstraint("tenant_id", "code", name="uq_regions_tenant_code"),)

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
    code: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    sort_order: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("0"),
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
