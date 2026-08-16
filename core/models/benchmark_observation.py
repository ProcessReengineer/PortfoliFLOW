# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""BenchmarkObservation ORM model — daily benchmark period-return time series.

Backs the ``benchmark_observations`` table introduced in
migration b011 (per ADR-0061 §Decision). Each row is one
``(benchmark, as_of_date)`` period-return observation. The
``period_return`` is a signed decimal (e.g. ``0.005`` = 0.5%
daily return), matching the convention of the existing
``total return actual`` Excel sheet.

``tenant_id`` is denormalised from ``benchmarks.tenant_id`` per
ADR-0035 §3 so RLS evaluates row-locally. ``(benchmark_id,
as_of_date)`` is UNIQUE. High-frequency table — no audit trigger
(analogous to ``investment_navs``).
"""

from __future__ import annotations

from datetime import date as _date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    Date,
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


class BenchmarkObservation(Base):
    """One ``(benchmark, as_of_date, period_return)`` triple."""

    __tablename__ = "benchmark_observations"
    __table_args__ = (
        UniqueConstraint(
            "benchmark_id",
            "as_of_date",
            name="uq_benchmark_observations_benchmark_date",
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
    benchmark_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("benchmarks.id", ondelete="CASCADE"),
        nullable=False,
    )
    as_of_date: Mapped[_date] = mapped_column(Date, nullable=False)
    period_return: Mapped[Decimal] = mapped_column(Numeric(20, 10), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
