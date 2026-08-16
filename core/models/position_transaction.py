# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""PositionTransaction ORM model — the transaction ledger.

Backs the ``position_transactions`` table introduced in migration b024
(per ADR-0097 §2). One row per position-changing event; holdings follow
deterministically as the cumulative signed sum of ``units`` ordered by
``(trade_date, created_at, id)`` — the ledger is the single source of
truth for unit counts and there is no ``holdings`` snapshot table
(ADR-0097 §4).

Signed quantities keep holdings derivation a plain cumulative sum. The
sign rules are CHECK-enforced: ``opening``/``buy`` require ``units > 0``,
``sell`` requires ``units < 0``, ``transfer`` requires ``units <> 0``.
``price_per_unit``, when present, must be ``> 0``; ``buy``/``sell``
require a price, ``opening``/``transfer`` may omit it (an
Excel-synthesised opening or an in-kind transfer has no trade price). A
partial unique index (``uq_position_transactions_opening``) enforces at
most one ``opening`` per investment.

``tenant_id`` is denormalised from ``investments`` per ADR-0035 §3 so RLS
evaluates row-locally without a JOIN. ``ingest_origin`` uses the uniform
ADR-0092 triple (``'excel'`` | ``'live'`` | ``'manual'``) for consistency
across the write-path families, even though no ``'live'`` transaction
writer exists yet. Distributions and dividends remain **cashflows, never
unit operations** (``investment_cashflows``); this ledger records only
position-changing events.
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
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from core.models.base import Base


class PositionTransaction(Base):
    """One position-changing event for one investment."""

    __tablename__ = "position_transactions"
    __table_args__ = (
        CheckConstraint(
            "txn_type IN ('opening', 'buy', 'sell', 'transfer')",
            name="ck_position_transactions_txn_type",
        ),
        CheckConstraint(
            "(txn_type IN ('opening', 'buy') AND units > 0) "
            "OR (txn_type = 'sell' AND units < 0) "
            "OR (txn_type = 'transfer' AND units <> 0)",
            name="ck_position_transactions_sign",
        ),
        CheckConstraint(
            "price_per_unit IS NULL OR price_per_unit > 0",
            name="ck_position_transactions_price_positive",
        ),
        CheckConstraint(
            "txn_type NOT IN ('buy', 'sell') OR price_per_unit IS NOT NULL",
            name="ck_position_transactions_price_required",
        ),
        CheckConstraint(
            "ingest_origin IN ('excel', 'live', 'manual')",
            name="ck_position_transactions_ingest_origin",
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
    txn_type: Mapped[str] = mapped_column(Text, nullable=False)
    trade_date: Mapped[_date] = mapped_column(Date, nullable=False)
    units: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    price_per_unit: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    consideration: Mapped[Decimal | None] = mapped_column(Numeric(20, 4), nullable=True)
    currency: Mapped[str] = mapped_column(Text, nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
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
