# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""InvestmentNav ORM model — date-stamped valuations per investment.

Backs the ``investment_navs`` table introduced in migration b006
(per ADR-0043 §1). Each row is one valuation point for one
investment on one statement day, carrying the ``nav_kind``
discriminator (``plan`` | ``actual``). Plan and actual series are
stored in parallel; the unique index is keyed on
``(investment_id, as_of_date, nav_kind)`` so plan and actual on the
same day coexist as two distinct rows.

``as_of_date`` is a ``DATE`` (statement-day semantics) rather than a
``TIMESTAMPTZ``: NAV reporting at p&p is end-of-day and a precise
intra-day timestamp would invent precision the source data does not
have. ``tenant_id`` is denormalised from ``investments`` per
ADR-0035 §3 so RLS evaluates row-locally without a JOIN.

``basis`` is the reported-vs-computed discriminator introduced by
ADR-0079 (additive): ``'reported'`` is a manager-supplied NAV,
``'computed'`` a future holdings-aggregation result. It is **nullable
with no backfill** — a NULL ``basis`` is treated as ``'reported'`` by
downstream code for backward compatibility. It is orthogonal to the
pre-existing free-text ``source`` provenance column, which is left
untouched.

``ingest_origin`` (b021 / ADR-0092) is the **producer that wrote the
row** — one of ``'excel'`` | ``'live'`` | ``'manual'`` | ``'system'``.
It is distinct from both ``basis`` (analytics semantics) and ``source``
(free-text provenance): it is the field the Excel-precedence guard
decides on. A live fetch overwrites no row whose origin is ``'excel'``
or ``'manual'`` (ADR-0092). ``'system'`` (b025 / ADR-0098 §1) marks a
row written by the platform's computed-NAV materialisation service
(``holdings × price``); precedence, strongest first, is ``'excel'`` >
``'manual'`` > ``'system'``, and the materialisation refreshes only its
own ``'system'`` rows. ``'system'`` stays orthogonal to
``basis='computed'``: the former is the writer channel, the latter is
how the number was formed (ADR-0079). NOT NULL, no server default —
every write states its origin.
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


class InvestmentNav(Base):
    """One date-stamped valuation row for one investment."""

    __tablename__ = "investment_navs"
    __table_args__ = (
        CheckConstraint(
            "nav_kind IN ('plan', 'actual')",
            name="ck_investment_navs_nav_kind",
        ),
        CheckConstraint(
            "basis IS NULL OR basis IN ('reported', 'computed')",
            name="ck_investment_navs_basis",
        ),
        CheckConstraint(
            "ingest_origin IN ('excel', 'live', 'manual', 'system')",
            name="ck_investment_navs_ingest_origin",
        ),
        UniqueConstraint(
            "investment_id",
            "as_of_date",
            "nav_kind",
            name="uq_investment_navs_investment_date_kind",
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
    nav_value: Mapped[Decimal] = mapped_column(Numeric(20, 4), nullable=False)
    currency: Mapped[str] = mapped_column(Text, nullable=False)
    nav_kind: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str | None] = mapped_column(Text, nullable=True)
    basis: Mapped[str | None] = mapped_column(Text, nullable=True)
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
