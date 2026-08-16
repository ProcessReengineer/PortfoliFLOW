# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tenant ORM model — the organisational boundary of data isolation.

Per ADR-0035, every domain table carries ``tenant_id`` and is policed
by Row-Level Security. The ``tenants`` table itself holds the tenant
definitions; its RLS policy permits a tenant to see only its own row
(``id = current_setting('app.tenant_id')::uuid``).

New tenant rows are inserted by the superuser path (Alembic seed
migrations or a future bootstrap CLI). The application's
``portfoliflow_app`` role cannot create tenants — that workflow is a
Phase-5 deliverable.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, String, Text, func, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from core.models.base import Base


class Tenant(Base):
    """A tenant — an organisational unit owning a self-contained dataset."""

    __tablename__ = "tenants"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=text("gen_random_uuid()"),
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # The tenant's reporting currency (ADR-0099 §1): every aggregate figure
    # the system reports is expressed in it. ISO 4217, upper-cased,
    # application-validated — the currency stammtabelle remains deferred, so
    # there is no CHECK and no FK. Distinct from an investment's *position*
    # currency and from the FX dataset's *reference* currency.
    functional_currency: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'EUR'")
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
