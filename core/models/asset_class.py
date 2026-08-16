# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Asset-class ORM model — per-tenant catalogue of asset-class definitions.

Backs the ``asset_classes`` table introduced in migration b005 (per
ADR-0042 §1). Each tenant curates its own asset-class vocabulary;
there is no global asset-class table. ``code`` is the short
identifier (e.g. ``"global_equity"``); ``display_name`` is the
human-readable label used in the SAA web UI.

The Phase-3 model intentionally has no ORM ``relationship()`` to
``SAAAssetClassInput`` or ``SAACorrelation``. Phase 3 is repository-
flavoured: cross-table reads are orchestrated in
``services/saa/saa_service.py``, not via lazy-loaded ORM relations.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
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


class AssetClass(Base):
    """One asset-class definition belonging to exactly one tenant."""

    __tablename__ = "asset_classes"
    __table_args__ = (UniqueConstraint("tenant_id", "code", name="uq_asset_classes_tenant_code"),)

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
