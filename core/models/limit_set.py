# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""LimitSet ORM model — tenant-scoped (family, effective_from) catalogue.

Backs the ``limit_sets`` table introduced in migration b010 (per
ADR-0056 §Schema). A limit set is the immutable container for one
generation of caps belonging to a regulatory or operational family
(``'saa'`` or ``'anlv'``). Selection at evaluation time is
``MAX(effective_from) WHERE effective_from <= as_of_date`` filtered
by ``family`` and ``tenant_id``.

Once persisted, the rows belonging to a set are never modified; a
correction requires a new set with a later ``effective_from``. The
b001 audit trigger captures any future operator-driven label/notes
edits for free.
"""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    Date,
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


class LimitSet(Base):
    """One (family, effective_from) limit-set generation per tenant."""

    __tablename__ = "limit_sets"
    __table_args__ = (
        CheckConstraint("family IN ('saa', 'anlv')", name="ck_limit_sets_family"),
        UniqueConstraint(
            "tenant_id",
            "family",
            "effective_from",
            name="uq_limit_sets_tenant_family_effective_from",
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
    family: Mapped[str] = mapped_column(Text, nullable=False)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
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
