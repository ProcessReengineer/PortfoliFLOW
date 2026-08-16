# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""InstrumentPrice ORM model — the per-unit price series.

Backs the ``instrument_prices`` table introduced in migration b024 (per
ADR-0097 §3). One row per ``(investment_id, as_of_date)`` — the
``uq_instrument_prices_investment_date`` unique constraint enforces the
natural key. The keying deliberately mirrors ``investment_navs``: a
per-investment price series with statement-day (``DATE``) granularity and
**no kind dimension** — prices are actuals; plan / scenario price paths
are ADR-C workspace concerns and never live here.

There is **one canonical price, no ``price_kind``** (close / bid / ask):
the pinned basis is the provider's daily valuation price — Yahoo
unadjusted EOD close, Bloomberg ``PX_LAST`` — exactly what the market-data
adapters already normalise (ADR-0091). Bid/ask/mid is a named successor
concern with no current consumer (YAGNI).

``tenant_id`` is denormalised from ``investments`` per ADR-0035 §3 so RLS
evaluates row-locally without a JOIN. ``ingest_origin`` carries the
ADR-0092 precedence field (``'excel'`` | ``'live'`` | ``'manual'``); the
repository's ``upsert_live`` refreshes only prior ``'live'`` rows and
never mutates an ``'excel'``/``'manual'`` price. Materialisation of NAV
rows from holdings × price is ADR-0098's concern (strand S2); this table
is only the price source.
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


class InstrumentPrice(Base):
    """One per-unit price for one investment on one statement day."""

    __tablename__ = "instrument_prices"
    __table_args__ = (
        CheckConstraint(
            "price > 0",
            name="ck_instrument_prices_price_positive",
        ),
        CheckConstraint(
            "ingest_origin IN ('excel', 'live', 'manual')",
            name="ck_instrument_prices_ingest_origin",
        ),
        UniqueConstraint(
            "investment_id",
            "as_of_date",
            name="uq_instrument_prices_investment_date",
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
    price: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    currency: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str | None] = mapped_column(Text, nullable=True)
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
