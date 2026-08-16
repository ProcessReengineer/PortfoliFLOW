# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Sector ORM model — per-tenant catalogue of sector definitions.

Backs the ``sectors`` table introduced in migration b007 (per
ADR-0045 §2). Each tenant curates its own sector vocabulary; there
is no global sector table. ``code`` is the short identifier (e.g.
``"tech_software"``); ``display_name`` is the human-readable label.

The ``unclassified`` row is installed per-tenant by
``portfoliflow bootstrap`` and supplies a fallback bucket for the
Excel-import path when an investment's ``Sector`` cell is empty
(mirroring the asset-class pattern from ADR-0043 §1).
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from core.models.base import Base


class Sector(Base):
    """One sector definition belonging to exactly one tenant."""

    __tablename__ = "sectors"
    __table_args__ = (UniqueConstraint("tenant_id", "code", name="uq_sectors_tenant_code"),)

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
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("TRUE"),
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
