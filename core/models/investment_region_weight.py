# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""InvestmentRegionWeight ORM model — per-investment region allocation.

Backs the ``investment_region_weights`` table introduced in migration
b009 (per ADR-0046) and **historised** by ADR-0080. Each row records
the share of one investment allocated to one region on one statement
day.

The natural key is now ``(investment_id, as_of_date, region_id)`` —
enforced by the
``uq_investment_region_weights_investment_date_region`` unique
constraint. ADR-0080 promoted ``as_of_date`` into the natural key so
the table is **time-series**, mirroring ``investment_rating_weight``
(ADR-0079 §2) rather than the prior point-in-time shape: a new
statement period lays down a new snapshot instead of destroying the
prior generation.

``weight_pct`` is a percentage in the closed interval ``[0, 100]``;
the constraint is enforced as a DB-side ``CHECK``. ``basis`` is the
reported-vs-computed discriminator (ADR-0079 / ADR-0080), NOT NULL —
every weight row states its provenance.

Two cosmetic asymmetries with the sibling weight tables are left
intact by ADR-0080 (§Scope boundaries): ``weight_pct`` keeps its
``NUMERIC(8, 4)`` precision (vs ``(7, 4)`` on sector/country) and the
table keeps its lack of an ``updated_at`` column (the block-replace
write needs no in-place update). The Excel import path writes this
table; the sibling ``investment_country_weights`` table is reserved
for ISO-granular data sources (GP report scrapers — roadmap A2/A3).
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
from sqlalchemy.orm import Mapped, mapped_column

from core.models.base import Base


class InvestmentRegionWeight(Base):
    """One region-allocation row for a single investment on one date."""

    __tablename__ = "investment_region_weights"
    __table_args__ = (
        CheckConstraint(
            "weight_pct >= 0",
            name="ck_region_weight_non_negative",
        ),
        CheckConstraint(
            "weight_pct <= 100",
            name="ck_region_weight_max",
        ),
        CheckConstraint(
            "basis IN ('reported', 'computed')",
            name="ck_investment_region_weights_basis",
        ),
        CheckConstraint(
            "ingest_origin IN ('excel', 'live', 'manual')",
            name="ck_investment_region_weights_ingest_origin",
        ),
        UniqueConstraint(
            "investment_id",
            "as_of_date",
            "region_id",
            name="uq_investment_region_weights_investment_date_region",
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
    region_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("regions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    weight_pct: Mapped[Decimal] = mapped_column(Numeric(8, 4), nullable=False)
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
