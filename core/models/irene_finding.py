# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""IreneFinding ORM model — append-only findings / journal.

Backs the ``irene_finding`` table introduced in migration b019 (per
ADR-0085 §``irene_finding``). Each row is a surfaced decision-support
card with a lifecycle: born on a rising edge, assigned a user
resolution, completed as an immutable audit record. The table is
append-only — a finding is never mutated except to record its
resolution.

``subject_key`` references the monitored subject but is deliberately
**not** a foreign key to ``irene_watch_state``: findings outlive state
rows, and RSS-only findings may reference transient buckets that never
acquire a watch-state row.

``payload`` holds the ``surface_finding`` contract (ADR-0088: trigger,
finding, basis, urgency_suggestion, options, evidence_refs); it is
opaque to the persistence layer. ``urgency`` is the *final* urgency
after the deterministic floor (ADR-0088), not Irene's suggestion, and
``band`` is derived from that final urgency. Resolution values are the
lowercase vocabulary ``open`` / ``acted`` / ``dismissed`` /
``acknowledged`` / ``opened_case``, enforced in application code, not as
a SQL enum. ``opened_case`` (ADR-0107, C4) records a hand-over to a Case
and is written only by the case-opening composition, never by the
Watch Desk's resolve endpoint.

There is intentionally no ``updated_at``: the only permitted mutation
is writing the resolution fields, and the append-only audit trail
records the ``created_at`` of the card itself.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    DateTime,
    ForeignKey,
    SmallInteger,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from core.models.base import Base


class IreneFinding(Base):
    """One append-only decision-support finding under a tenant context."""

    __tablename__ = "irene_finding"

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
    # Reference to the subject, NOT an FK to irene_watch_state: findings
    # outlive state rows and RSS-only findings reference transient buckets.
    subject_key: Mapped[str] = mapped_column(Text, nullable=False)
    # The surface_finding contract (ADR-0088); opaque to persistence.
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # Final urgency after the deterministic floor (ADR-0088), not Irene's
    # suggestion. Opaque to persistence.
    urgency: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    band: Mapped[str] = mapped_column(Text, nullable=False)
    resolution: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="open",
        server_default=text("'open'"),
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # A user id when known; no FK required (a finding may be resolved by a
    # process or a since-deleted user).
    resolved_by: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
