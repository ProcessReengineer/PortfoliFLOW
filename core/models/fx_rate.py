# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""FxRate ORM model — the FX-rate series behind the functional-currency model.

Backs the ``fx_rates`` table introduced in migration b026 (per ADR-0099 §2).
One row per ``(tenant_id, currency, as_of_date)`` — the
``uq_fx_rates_tenant_currency_date`` unique constraint enforces the natural
key.

**Quoting convention (normative, ADR-0099 §2):** ``rate_to_reference`` is
the price of one unit of ``currency`` in the reference currency. In an
EUR-based deployment ``USD → 0.92`` means 1 USD = 0.92 EUR. Conversion
between two non-reference currencies triangulates as
``amount × rate(from) / rate(to)``, which keeps the dataset linear rather
than quadratic in the number of currencies.

Three currency concepts meet in this table and must not be conflated:

* **functional currency** — the tenant's reporting currency
  (``tenants.functional_currency``). Not stored here.
* **position currency** — an investment's own currency
  (``investments.currency``). Not stored here.
* **reference currency** — the base of this FX dataset, stored per row in
  ``reference_currency`` so every rate is self-describing for audit.
  Constant per tenant in practice; a property of the data, not of the
  portfolio.

The identity rate is **never** a row: ``ck_fx_rates_currency_not_reference``
forbids ``currency = reference_currency``, because ``rate(reference) = 1``
is an application-level short-circuit. That short-circuit is the
backwards-compatibility guarantee — a single-currency tenant needs zero FX
rows (ADR-0099 §3).

``tenant_id`` is carried on the row per ADR-0035 §3 so RLS evaluates
row-locally without a JOIN; FX rates are tenant-scoped rather than global
because the ADR-0092 Excel-over-live precedence is inherently
tenant-specific (ADR-0099 §2). ``ingest_origin`` carries that precedence
field (``'excel'`` | ``'live'`` | ``'manual'``); the repository's
``upsert_live`` refreshes only prior ``'live'`` rows and never mutates an
``'excel'``/``'manual'`` rate.

No ``relationship()`` traversals are declared: repositories join
explicitly, per the Phase-3/4 convention.
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


class FxRate(Base):
    """One FX rate for one currency on one date, quoted against a reference."""

    __tablename__ = "fx_rates"
    __table_args__ = (
        CheckConstraint(
            "rate_to_reference > 0",
            name="ck_fx_rates_rate_positive",
        ),
        CheckConstraint(
            "currency <> reference_currency",
            name="ck_fx_rates_currency_not_reference",
        ),
        CheckConstraint(
            "ingest_origin IN ('excel', 'live', 'manual')",
            name="ck_fx_rates_ingest_origin",
        ),
        UniqueConstraint(
            "tenant_id",
            "currency",
            "as_of_date",
            name="uq_fx_rates_tenant_currency_date",
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
    as_of_date: Mapped[_date] = mapped_column(Date, nullable=False)
    currency: Mapped[str] = mapped_column(Text, nullable=False)
    rate_to_reference: Mapped[Decimal] = mapped_column(Numeric(20, 10), nullable=False)
    reference_currency: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
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
