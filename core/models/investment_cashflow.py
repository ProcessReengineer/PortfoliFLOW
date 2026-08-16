# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""InvestmentCashflow ORM model — cashflow events per investment.

Backs the ``investment_cashflows`` table introduced in migration
b006 (per ADR-0043 §1). Each row is one cashflow event for one
investment carrying the ``flow_type`` discriminator (one of eight
values: ``capital_call``, ``distribution``, ``fee``, ``carry``,
``dividend``, ``coupon``, ``other``, ``investor_flow``) and the
``flow_kind`` discriminator (``plan`` | ``actual``).

``investor_flow`` (b028 / ADR-0103 §5) is the eighth value: a net
contribution to, or withdrawal from, the mandate. It is bookable on
**cash positions only** — the investment row of the currency the flow
settles in (ADR-0103 §5, decision N4). That rule spans two tables and
is therefore enforced at the service seam
(:meth:`services.investments.InvestmentService.add_cashflow` /
:meth:`~services.investments.InvestmentService.update_cashflow`), not by
a CHECK. Both ``flow_kind`` variants are legal and the amount is signed
in both directions: ``plan`` investor flows feed the cash plan path
(ADR-0103 §6), ``actual`` ones are informational — actual balances come
exclusively from statement levels, so no double count is possible.

``flow_timestamp`` is a ``TIMESTAMPTZ`` with the operational
convention that 12:00 UTC is used when the precise time is unknown.
There is **no UNIQUE constraint** — multiple cashflows per
investment / timestamp / type / kind are permitted, matching the
operational reality that several capital calls or fee payments can
share the same day. ``tenant_id`` is denormalised from
``investments`` per ADR-0035 §3 so RLS evaluates row-locally
without a JOIN.

Sign convention: ``amount`` is signed — capital calls and fees are
negative, distributions and coupons are positive — but the database
imposes no sign CHECK because real-world data occasionally arrives
with corrections that flip a sign legitimately.

``source`` (b021 / ADR-0092) is a nullable free-text provenance column
mirroring ``investment_navs.source`` — the live write path records its
provider name here; pre-live rows are honestly NULL. ``ingest_origin``
(b021 / ADR-0092) is the **producer that wrote the row**
(``'excel'`` | ``'live'`` | ``'manual'``) — the field the
Excel-precedence guard decides on. Because the table has no unique key
(multiple same-day flows are legitimate, ADR-0043 §1), live-ingest
idempotency uses a deterministic dedup key computed rule-based from
``(investment_id, flow_timestamp, flow_type, flow_kind, amount,
source)`` (see :mod:`services.investments.cashflow_dedup_key`), never a
DB constraint. NOT NULL, no server default — every write states its
origin.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
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


class InvestmentCashflow(Base):
    """One cashflow event row for one investment."""

    __tablename__ = "investment_cashflows"
    __table_args__ = (
        CheckConstraint(
            "flow_type IN ("
            "'capital_call', 'distribution', 'fee', 'carry', "
            "'dividend', 'coupon', 'other', 'investor_flow'"
            ")",
            name="ck_investment_cashflows_flow_type",
        ),
        CheckConstraint(
            "flow_kind IN ('plan', 'actual')",
            name="ck_investment_cashflows_flow_kind",
        ),
        CheckConstraint(
            "ingest_origin IN ('excel', 'live', 'manual')",
            name="ck_investment_cashflows_ingest_origin",
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
    flow_timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    flow_type: Mapped[str] = mapped_column(Text, nullable=False)
    flow_kind: Mapped[str] = mapped_column(Text, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(20, 4), nullable=False)
    currency: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
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
