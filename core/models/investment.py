# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Investment ORM model — the flat-polymorphic investment instrument table.

Backs the ``investments`` table introduced in migration b006 (per
ADR-0043 §1). One row per investment instrument carrying the
``investment_type`` discriminator (one of eight allowed values — the
original seven plus ``'cash'``, added by ADR-0100 §1 / migration b027), a
1:1 foreign key to the per-tenant ``asset_classes`` catalogue, the
core operational metadata (manager, region, currency, vintage,
commitment), and an ``is_active`` flag that the Excel-import
workflow (sub-stream 4c) toggles for soft-delete-with-reactivation
semantics.

The ``type_specific_data`` JSONB column is reserved as an emergency
exit for Phase-5+ extensions but is **unused in Phase 4**: all seven
investment types share the same column structure, and side tables
are deferred until type-specific behaviour is modelled (see
ADR-0043 §2).

The ``anlv_code`` column (b010) is a nullable text FK to the global
``anlv_categories`` catalogue (ADR-0057). ``NULL`` represents the
"AnlV unallocated" engine-fallback case — investments without a
regulatory classification contribute to a synthetic unallocated
bucket at coverage-evaluation time.

Phase-4 modelling style follows the Phase-3 repository-flavoured
convention: this module deliberately does not expose ORM
``relationship()`` traversals to ``InvestmentNav`` or
``InvestmentCashflow``. Cross-table reads are orchestrated in
``services/investments/investment_service.py``.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from core.models.base import Base

# The eight canonical ``investment_type`` discriminator values (ADR-0043,
# extended by ADR-0100 §1). Mirrors the DB CHECK on
# ``investments.investment_type``; the set is extended only by a successor
# ADR + migration. ``cash`` is the eighth, added so a foreign-currency cash
# balance can be a first-class investment row.
INVESTMENT_TYPES: frozenset[str] = frozenset(
    {
        "private_equity",
        "private_debt",
        "real_estate",
        "infra_equity",
        "listed_equity",
        "listed_bonds",
        "other",
        "cash",
    }
)


class Investment(Base):
    """One investment instrument belonging to exactly one tenant."""

    __tablename__ = "investments"
    __table_args__ = (
        CheckConstraint(
            "investment_type IN ("
            "'private_equity', 'private_debt', 'real_estate', "
            "'infra_equity', 'listed_equity', 'listed_bonds', 'other', "
            "'cash'"
            ")",
            name="ck_investments_investment_type",
        ),
        CheckConstraint(
            "valuation_mode IN ('reported', 'unitised')",
            name="ck_investments_valuation_mode",
        ),
        UniqueConstraint("tenant_id", "name", name="uq_investments_tenant_name"),
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
    name: Mapped[str] = mapped_column(Text, nullable=False)
    investment_type: Mapped[str] = mapped_column(Text, nullable=False)
    asset_class_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("asset_classes.id", ondelete="RESTRICT"),
        nullable=False,
    )
    manager_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    region: Mapped[str | None] = mapped_column(Text, nullable=True)
    currency: Mapped[str] = mapped_column(Text, nullable=False)
    vintage_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    commitment_amount: Mapped[Decimal | None] = mapped_column(Numeric(20, 4), nullable=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("TRUE"),
    )
    # ADR-0097 §1: the per-investment write-path discriminator. 'reported'
    # (NAV carried directly in investment_navs, as today) or 'unitised' (NAV
    # materialised from holdings × price, ADR-0098). The DEFAULT is retained
    # (unlike b021's ingest_origin) — a new investment is 'reported' until an
    # operator explicitly flips it (strand S5). No reader of NAV series may
    # branch on this (the protected F3 seam).
    valuation_mode: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default=text("'reported'"),
    )
    type_specific_data: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    anlv_code: Mapped[str | None] = mapped_column(
        Text,
        ForeignKey("anlv_categories.code", ondelete="RESTRICT"),
        nullable=True,
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
