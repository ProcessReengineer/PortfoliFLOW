# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""InvestmentIdentifier ORM model — security identifiers per investment.

Backs the ``investment_identifiers`` table introduced in migration
b020 (per ADR-0090 §Decision) and extended by migration b023 (ADR-0096).
Each row records one security identifier — an ISIN, ticker, FIGI, CUSIP,
free-namespace ``internal`` code, or provider-native private-markets fund
scheme (``preqin`` / ``pitchbook``) — for one investment. Identifiers are the
deterministic join-key that makes an existing investment addressable
against external market-data providers (the first slice of Live Data
Import, roadmap #036).

Modelling notes (ADR-0090):

- Identity is ``(investment_id, scheme, value)`` — an investment may
  hold ISIN + ticker + FIGI simultaneously, so identifiers live in a
  child table rather than as columns on ``investments``. A
  private-markets instrument with no market identifier simply carries
  zero rows here, which is exactly what excludes it from live import
  (no NULL-column ambiguity).
- ``scheme`` is a plain ``TEXT`` with a CHECK over the closed set
  ``('isin','ticker','figi','cusip','internal','preqin','pitchbook')``
  (ADR-0096) — no SQL enum, matching the codebase's TEXT-for-status
  convention. The canonical vocabulary is mirrored here as
  :data:`IDENTIFIER_SCHEMES` for application-side reference.
- ``value`` is normalised (trimmed + upper-cased) by the repository on
  write; the DB only guards non-emptiness. No scheme-specific format
  validation (ISIN checksums etc.) is imposed at the schema layer.
- ``tenant_id`` is denormalised from ``investments`` (ADR-0035 §3) so
  RLS evaluates row-locally without a JOIN. ``investment_id`` carries
  ``ON DELETE CASCADE``; ``tenant_id`` and ``created_by`` are
  ``RESTRICT``.

Uniqueness (all three from ADR-0090 §Decision):

- ``UNIQUE (investment_id, scheme, value)`` — no duplicate identifier
  per investment.
- Partial ``UNIQUE (tenant_id, scheme, value) WHERE scheme <>
  'internal'`` — a real-world identifier maps to at most one
  investment within a tenant.
- Partial ``UNIQUE (investment_id) WHERE is_primary`` — at most one
  primary identifier per investment.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.models.base import Base

# Canonical identifier schemes (ADR-0090 §Decision, extended by ADR-0096).
# Mirrors the DB CHECK on ``investment_identifiers.scheme``; the set is
# extended only by a successor ADR + migration. ``internal`` is a free
# operator namespace and is exempt from the per-tenant uniqueness rule.
# ``preqin`` / ``pitchbook`` are provider-native private-markets fund
# schemes added by ADR-0096 (migration b023) — a private-equity fund has no
# ISIN/ticker/FIGI, so its provider ID is the only join-key it can carry.
IDENTIFIER_SCHEMES: frozenset[str] = frozenset(
    {"isin", "ticker", "figi", "cusip", "internal", "preqin", "pitchbook"}
)


class InvestmentIdentifier(Base):
    """One security identifier (ISIN / ticker / FIGI / …) for an investment."""

    __tablename__ = "investment_identifiers"
    __table_args__ = (
        CheckConstraint(
            "scheme IN ('isin', 'ticker', 'figi', 'cusip', 'internal', 'preqin', 'pitchbook')",
            name="ck_investment_identifiers_scheme",
        ),
        # Non-emptiness only; format validation is an application concern.
        CheckConstraint(
            "char_length(btrim(value)) > 0",
            name="ck_investment_identifiers_value_nonempty",
        ),
        UniqueConstraint(
            "investment_id",
            "scheme",
            "value",
            name="uq_investment_identifiers_investment_scheme_value",
        ),
        # A real-world identifier maps to at most one investment per tenant;
        # 'internal' is a free namespace and is excluded.
        Index(
            "uq_investment_identifiers_tenant_scheme_value",
            "tenant_id",
            "scheme",
            "value",
            unique=True,
            postgresql_where=text("scheme <> 'internal'"),
        ),
        # At most one primary identifier per investment.
        Index(
            "uq_investment_identifiers_primary_per_investment",
            "investment_id",
            unique=True,
            postgresql_where=text("is_primary"),
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
    # One of IDENTIFIER_SCHEMES; enforced by the DB CHECK, not a SQL enum.
    scheme: Mapped[str] = mapped_column(Text, nullable=False)
    # Normalised (trimmed + upper-cased) on write by the repository.
    value: Mapped[str] = mapped_column(Text, nullable=False)
    is_primary: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("FALSE"),
    )
    # Free-text provenance ('excel', 'openfigi', 'manual'); nullable.
    source: Mapped[str | None] = mapped_column(Text, nullable=True)
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
        backref="identifiers",
        lazy="raise",
    )
