# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""InvestmentRatingWeight ORM model — per-investment credit-rating mix.

Backs the ``investment_rating_weight`` time-series table introduced
by ADR-0079 §2. Each row records the share of one ``listed_bonds``
investment allocated to one credit-rating bucket on one statement
day.

The natural key is ``(investment_id, as_of_date, rating_bucket)`` —
enforced by the ``uq_investment_rating_weight_investment_date_bucket``
unique constraint. Unlike the point-in-time
``investment_sector_weights`` (natural key ``(investment_id,
sector_id)``, no ``as_of_date``), this table is **time-series**:
ADR-0079 §2 historises FI composition because it shifts materially
over a fund's life and the Fixed-Income archetype shows it over
time. ``tenant_id`` is denormalised from ``investments`` per
ADR-0035 §3 for row-local RLS.

Domain notes (ADR-0079 §2):

- ``rating_bucket`` is **constrained text** (the ``flow_type`` /
  ``nav_kind`` pattern), not a reference-table FK: the eight buckets
  (``AAA``, ``AA``, ``A``, ``BBB``, ``BB``, ``B``,
  ``CCC_and_below``, ``NR``) are small, fixed, and canonical. The
  headline average rating is derived notch-weighted on read
  (ADR-0079 §3), never a naive mean.
- ``weight_pct`` is a percentage in the closed interval ``[0, 100]``,
  matching ``investment_sector_weights.weight_pct`` precision
  (``Numeric(7, 4)``); weights need not sum to 100.
- ``basis`` is the reported-vs-computed discriminator (ADR-0079).
  NOT NULL here — every weight row states its provenance — in
  contrast to ``investment_navs.basis`` where NULL ⇒ ``reported``.
"""

from __future__ import annotations

from datetime import date as _date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Numeric,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.models.base import Base


class InvestmentRatingWeight(Base):
    """One credit-rating-bucket weight row for a single investment."""

    __tablename__ = "investment_rating_weight"
    __table_args__ = (
        CheckConstraint(
            "rating_bucket IN ('AAA', 'AA', 'A', 'BBB', 'BB', 'B', 'CCC_and_below', 'NR')",
            name="ck_investment_rating_weight_rating_bucket",
        ),
        CheckConstraint(
            "weight_pct >= 0 AND weight_pct <= 100",
            name="ck_investment_rating_weight_weight_pct_range",
        ),
        CheckConstraint(
            "basis IN ('reported', 'computed')",
            name="ck_investment_rating_weight_basis",
        ),
        CheckConstraint(
            "ingest_origin IN ('excel', 'live', 'manual')",
            name="ck_investment_rating_weight_ingest_origin",
        ),
        UniqueConstraint(
            "investment_id",
            "as_of_date",
            "rating_bucket",
            name="uq_investment_rating_weight_investment_date_bucket",
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
    investment_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("investments.id", ondelete="CASCADE"),
        nullable=False,
    )
    as_of_date: Mapped[_date] = mapped_column(Date, nullable=False)
    rating_bucket: Mapped[str] = mapped_column(Text, nullable=False)
    weight_pct: Mapped[Decimal] = mapped_column(Numeric(7, 4), nullable=False)
    basis: Mapped[str] = mapped_column(Text, nullable=False)
    ingest_origin: Mapped[str] = mapped_column(Text, nullable=False)
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

    investment = relationship(
        "Investment",
        backref="rating_weights",
        lazy="raise",
    )
