# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""InvestmentBondAnalytics ORM model — per-investment FI characteristics.

Backs the ``investment_bond_analytics`` time-series table introduced
by ADR-0079 §2. Each row records the fixed-income characteristics
(yield-to-maturity, effective duration, option-adjusted spread,
convexity) of one ``listed_bonds`` investment on one statement day.

The natural key is ``(investment_id, as_of_date)`` — one
characteristics snapshot per investment per day — enforced by the
``uq_investment_bond_analytics_investment_date`` unique constraint.
``as_of_date`` is a ``DATE`` (statement-day semantics), mirroring
``investment_navs`` (ADR-0043 §1). ``tenant_id`` is denormalised
from ``investments`` per ADR-0035 §3 so RLS evaluates row-locally
without a JOIN.

Domain notes (ADR-0079 §2/§3):

- ``ytm``, ``eff_duration``, ``oas`` and ``convexity`` carry **no
  value or sign CheckConstraint**: yields and spreads can legitimately
  be negative (e.g. EUR government bonds traded above par), so a
  ``>= 0`` guard would reject valid data.
- ``ytm`` and ``eff_duration`` are NOT NULL; ``oas`` and ``convexity``
  are nullable because not every manager reports all four.
- There is deliberately **no ``tr_index`` column**: total return is
  derived on read from the reported NAV (price) series and income
  flows (ADR-0079 §3, ADR-0013), never persisted.
- ``basis`` is the reported-vs-computed discriminator introduced by
  ADR-0079: ``'reported'`` is manager-supplied, ``'computed'`` is a
  future holdings-aggregation result. Unlike ``investment_navs.basis``
  (nullable, NULL ⇒ ``reported``), ``basis`` here is NOT NULL — every
  characteristics row states its provenance explicitly.
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


class InvestmentBondAnalytics(Base):
    """One fixed-income characteristics row for a single investment."""

    __tablename__ = "investment_bond_analytics"
    __table_args__ = (
        CheckConstraint(
            "basis IN ('reported', 'computed')",
            name="ck_investment_bond_analytics_basis",
        ),
        UniqueConstraint(
            "investment_id",
            "as_of_date",
            name="uq_investment_bond_analytics_investment_date",
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
    # No value/sign CheckConstraint: yields and spreads can be negative
    # (e.g. EUR govvies above par) — ADR-0079 §2.
    ytm: Mapped[Decimal] = mapped_column(Numeric(9, 6), nullable=False)
    eff_duration: Mapped[Decimal] = mapped_column(Numeric(6, 3), nullable=False)
    oas: Mapped[Decimal | None] = mapped_column(Numeric(9, 6), nullable=True)
    convexity: Mapped[Decimal | None] = mapped_column(Numeric(8, 3), nullable=True)
    basis: Mapped[str] = mapped_column(Text, nullable=False)
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
        backref="bond_analytics",
        lazy="raise",
    )
