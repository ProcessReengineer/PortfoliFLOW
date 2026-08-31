# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Trade-ticket ORM models — the Transactions area persistence (ADR-0128).

Backs the two tables introduced in migration b034: ``trade_tickets`` and
``trade_ticket_effects``. A trade ticket is the platform's first-class
record of a portfolio *change* — one intended or recorded change per row,
carried through one lifecycle from ``draft`` to ``booked`` or
``cancelled`` (ADR-0128 §1, §3).

Naming: the object is a **trade ticket** (institutional vocabulary),
which avoids the collision of "transaction" with ``position_transactions``
and with database transactions. The Area label stays *Transactions*
(ADR-0128 §1).

Two invariants are carried by the *shape* of these models as much as by
the CHECKs behind them:

* **The ledger stays ignorant of the layer above it** (ADR-0128 §2).
  Neither :class:`TradeTicket` nor :class:`TradeTicketEffect` is
  referenced by ``position_transactions``, ``investment_cashflows`` or
  ``investment_navs``, and there is deliberately no ORM relationship
  pointing that way. ``TradeTicketEffect.effect_id`` carries no foreign
  key at all — see its class docstring.
* **The investment row is an emission effect, not a precondition**
  (MD-12, decision record §2.5). ``investment_id`` is nullable and the
  master data lives in ``master_data`` until booking, so discarding a
  draft deletes the ticket and nothing else.

Status and the other vocabularies are TEXT, CHECK-constrained in the
schema and validated in the repository — never SQL enums, matching the
codebase's status convention throughout (b019/b020 precedent).
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    Date,
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


class TradeTicket(Base):
    """One tenant-scoped trade ticket — a recorded portfolio change.

    *Booked* and *proposed* are two stations of one lifecycle, not two
    object kinds (ADR-0128 §1): the realised-vs-intended comparison is
    the analytical value, and splitting the object would turn it into a
    join problem. ``status`` runs over the full eight-value vocabulary
    from day one — ``sent`` / ``acknowledged`` / ``executed`` are defined
    but unreachable in v1, and ADR-0129 arms them (ADR-0128 §3).

    ``ticket_number`` is a tenant-sequential display number allocated
    when the draft row is created, which is the first explicit user
    gesture and never the mere opening of a composer (MD-2, decision
    record §2.1). The ``(tenant_id, ticket_number)`` unique constraint is
    the race-safety guarantee for that allocation — the ``case_number``
    precedent, mechanics included.

    The four-eyes seam (D-4) is present as columns from this first
    migration: ``proposed_by`` / ``approved_by`` / ``booked_by`` with
    their timestamps. v1 permits ``approved_by == proposed_by``;
    enforcement arrives later as a tenant-scoped setting, so it is a rule
    change rather than a migration. Schema CHECKs guarantee only that a
    station's attribution columns are present once that station has been
    reached; *which* transitions are legal is service policy (ADR-0128
    §3), not a schema fact.

    ``master_data`` is JSONB and **opaque to persistence** — the
    finding-payload idiom (ADR-0088), reused. It carries the full U-NEW /
    R-COMMIT / R-SEC-BUY master-data inventory until the booking emission
    creates the ``investments`` row (MD-12 / MD-15, decision record
    §2.5), which is why ``investment_id`` is nullable.

    ``cash_investment_id`` is always an explicit, user-confirmed choice
    (D-1 / MD-3, §2.2): no default-selection logic exists anywhere, so
    NULL in a draft means "not yet confirmed", never "pick one for me".
    It is forced NULL for ``kind='commitment'``, which books no cash leg
    (R-3 / MD-19). ``settlement_date`` is captured but informational in
    v1 — both legs book at ``trade_date`` (MD-4, §2.3). ``set_inactive``
    is the home for the U-SELL full-disposal choice (MD-7, §2.6);
    R-SEC-SELL inactivates unconditionally (MD-17) and does not read it.

    ``units`` is held **unsigned**; the direction's sign is applied at
    emission (working document §1.1). Amount arithmetic
    (``net = gross ∓ fees ∓ taxes``) is a service concern and is
    deliberately not constrained here.
    """

    __tablename__ = "trade_tickets"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "ticket_number",
            name="uq_trade_tickets_tenant_ticket_number",
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
    # Tenant-sequential display number; uniqueness is the allocation's
    # race-safety guarantee (uq_trade_tickets_tenant_ticket_number).
    ticket_number: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    direction: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="draft",
        server_default=text("'draft'"),
    )
    # Null while a new-instrument draft is mid-wizard: the investments row
    # is an emission effect, not a precondition (MD-12).
    investment_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("investments.id", ondelete="RESTRICT"),
        nullable=True,
    )
    # Always an explicit, user-confirmed choice (D-1 / MD-3); forced NULL
    # for kind='commitment'.
    cash_investment_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("investments.id", ondelete="RESTRICT"),
        nullable=True,
    )
    # The booking date of both legs.
    trade_date: Mapped[date] = mapped_column(Date, nullable=False)
    # Recorded only; cash books on the trade date in v1 (MD-4).
    settlement_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    # Unsigned; the sign is applied at emission (working document §1.1).
    units: Mapped[Decimal | None] = mapped_column(Numeric(24, 8), nullable=True)
    price_per_unit: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    gross_amount: Mapped[Decimal | None] = mapped_column(Numeric(20, 4), nullable=True)
    fees: Mapped[Decimal | None] = mapped_column(Numeric(20, 4), nullable=True)
    taxes: Mapped[Decimal | None] = mapped_column(Numeric(20, 4), nullable=True)
    net_amount: Mapped[Decimal | None] = mapped_column(Numeric(20, 4), nullable=True)
    currency: Mapped[str] = mapped_column(Text, nullable=False)
    commitment_amount: Mapped[Decimal | None] = mapped_column(Numeric(20, 4), nullable=True)
    # The master-data inventory carried until booking emits the investments
    # row (MD-12 / MD-15); opaque to persistence.
    master_data: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # The U-SELL full-disposal choice (MD-7, §2.6).
    set_inactive: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("FALSE"),
    )
    # Free text, mirroring the ledger's own fields.
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str | None] = mapped_column(Text, nullable=True)
    cancel_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The Watch Desk -> Case -> Transactions provenance chain (ADR-0128 §1).
    # SET NULL: a deleted case must not take the change record with it.
    case_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("cases.id", ondelete="SET NULL"),
        nullable=True,
    )
    # The four-eyes seam (D-4): columns from day one, enforcement later.
    proposed_by: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )
    proposed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    approved_by: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    booked_by: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )
    booked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
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


class TradeTicketEffect(Base):
    """One ledger row a booked :class:`TradeTicket` emitted (ADR-0128 §2).

    The ticket records what it booked; **the ledger stays ignorant of the
    layer above it**. No column lands on ``position_transactions``,
    ``investment_cashflows`` or ``investment_navs``: emitted rows keep
    ``ingest_origin='manual'`` (Q-1), and this table is the authoritative
    linkage. Enumerating a ticket's effects is what makes them reversible
    and its provenance machine-readable.

    ``effect_id`` carries **no foreign key**, deliberately and on two
    grounds. A FK would be the upward coupling §2 forbids, and it would
    also be wrong on the facts: the referenced row may legitimately
    disappear — a reversal deletes it, or the ADR-0097 §7 per-investment
    CRUD corrects it — and the effect record must survive as history
    rather than block that delete or vanish with it. Resolving an
    ``effect_id`` is therefore a lookup that may legitimately come back
    empty; callers treat that as history, not as corruption.

    ``prior_state`` is the before-image of an updated ``investments`` row
    (D-2), populated only for ``effect_type='investment_update'`` so a
    reversal can restore what the booking changed. Like ``master_data``
    it is opaque JSONB — the shape of the before-image is the emitting
    service's contract, not this layer's.

    ``tenant_id`` is denormalised from the parent ticket per ADR-0035 §3
    so RLS evaluates row-locally without a JOIN, the same idiom as
    ``case_entries``.
    """

    __tablename__ = "trade_ticket_effects"
    __table_args__ = (
        UniqueConstraint(
            "ticket_id",
            "effect_type",
            "effect_id",
            name="uq_trade_ticket_effects_ticket_effect",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=text("gen_random_uuid()"),
    )
    # Denormalised from the parent trade_tickets row for row-local RLS
    # (ADR-0035 §3).
    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="RESTRICT"),
        nullable=False,
    )
    # CASCADE: the effect list is the ticket's own record of what it
    # emitted and has no meaning without it.
    ticket_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("trade_tickets.id", ondelete="CASCADE"),
        nullable=False,
    )
    effect_type: Mapped[str] = mapped_column(Text, nullable=False)
    # Deliberately NO foreign key — see the class docstring (ADR-0128 §2).
    effect_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    # The before-image of an updated investments row (D-2); opaque JSONB.
    prior_state: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    emitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
