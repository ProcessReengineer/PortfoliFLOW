# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""ORM model for the persistent DataStore-backed table.

Mirrors the in-memory ``DataStore`` API at the row level: each row is
one named DataFrame plus its metadata. The ``data`` column stores the
DataFrame as a JSONB payload (records-orientation, ISO-8601 dates) and
``meta`` stores the optional metadata dict.

This model is consumed only by :class:`core.persistent_data_store.PersistentDataStore`.
The Phase-1 factory ``get_data_store()`` still returns the in-memory
implementation; the persistent variant is exercised by tests but not
wired into the operational path. Phase 2 will switch the factory.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from core.models.base import Base


class DataStoreEntry(Base):
    """One named DataFrame stored under a tenant context."""

    __tablename__ = "data_store_entries"
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_data_store_entries_tenant_name"),
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
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    data: Mapped[dict] = mapped_column(JSONB, nullable=False)
    meta: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
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
