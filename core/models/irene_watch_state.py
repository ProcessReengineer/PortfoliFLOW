# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""IreneWatchState ORM model — typed world state for the Watch Desk.

Backs the ``irene_watch_state`` table introduced in migration b019
(per ADR-0085 §``irene_watch_state``). Irene, the Watch Desk's
proactive agent, writes one row per monitored subject per beat. The
row is the *functional prior* the heartbeat (ADR-0086) diffs the
current world against — not merely an audit trail.

Identity is ``(tenant_id, subject_key)``: a stable, deterministic,
rule-formed subject identifier (e.g. ``anlv:16``, ``saa:equity``,
``rss:cluster:<hash>``). ``magnitude`` stores the measured quantity
so a material escalation *within* an existing band (50.5% → 58%) is
distinguishable from noise (50.5% → 50.6%); it is nullable for
non-scalar subjects. The ``acknowledged_*`` pair records the state the
user has already seen — edge and re-trigger deltas are computed
against those fields (Prompt 3 / ADR-0086), never against the previous
raw beat, so the upsert on every beat must not clobber them.

``band`` is deterministically assigned (``informational`` /
``noteworthy`` / ``critical``), never LLM-set. The canonical vocabulary
is enforced in application code and tests, not as a SQL enum, matching
the codebase's TEXT-for-status convention.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
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


class IreneWatchState(Base):
    """Typed world state for one monitored subject, upserted per beat."""

    __tablename__ = "irene_watch_state"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "subject_key",
            name="uq_irene_watch_state_tenant_subject",
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
    subject_key: Mapped[str] = mapped_column(Text, nullable=False)
    # Nullable: non-scalar subjects have no single measured magnitude.
    magnitude: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    # Deterministically derived band; never LLM-set. Values:
    # 'informational' / 'noteworthy' / 'critical'.
    band: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The state the user has already seen. Written only by the delta
    # logic (Prompt 3); the per-beat upsert must leave these untouched.
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    acknowledged_magnitude: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
