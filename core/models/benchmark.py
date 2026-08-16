# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Benchmark ORM model — per-tenant catalogue of benchmark definitions.

Backs the ``benchmarks`` table introduced in migration b011 (per
ADR-0061 §Decision). Each tenant curates its own benchmark
vocabulary; there is no global benchmark table. ``code`` is the
short identifier (e.g. ``"BM_EQUITIES_DM"``); ``display_name`` is
the human-readable label rendered in the Benchmarks & Attribution
section. ``provider_hint`` documents the intended external data
source without coupling the schema to a specific vendor.

Per the Phase-3 repository-pattern convention this model carries
no ORM ``relationship()`` traversals to
:class:`BenchmarkObservation` or
:class:`AssetClassBenchmarkMapping`. Cross-table reads are
orchestrated at the service layer.
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


class Benchmark(Base):
    """One benchmark definition belonging to exactly one tenant."""

    __tablename__ = "benchmarks"
    __table_args__ = (UniqueConstraint("tenant_id", "code", name="uq_benchmarks_tenant_code"),)

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
    provider_hint: Mapped[str | None] = mapped_column(Text, nullable=True)
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
