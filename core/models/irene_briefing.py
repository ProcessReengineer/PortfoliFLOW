# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""IreneBriefing ORM model — the persisted artefact of one completed beat.

Backs the ``irene_briefing`` table introduced in migration b036 (per
ADR-0133 §1.1). One row per tenant and ``beat_at``: ``written`` with
Irene's two parts (``text_markets``, ``text_watch``), or ``skipped`` with
a ``skip_reason`` — in both cases with the ``inputs`` she was handed and
the ``finding_ids`` the beat appended. ``text_watch == ''`` is a valid
written part: every family switched off leaves nothing to say about the
watch.

The table is **append-only and immutable**. There is no ``updated_at``
and the repository offers append and reads only. Unlike
``watch_family_scope`` it carries **no audit trigger**: a briefing is a
system artefact written by the tick with no human actor, and the row
itself is the record (the ``irene_finding`` idiom). RLS scopes it to the
tenant.

``finding_ids`` are plain references, not foreign keys — as
``irene_finding.subject_key`` is a reference, not a key.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY as PG_ARRAY
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from core.models.base import Base

#: The briefing status vocabulary — mirrors
#: ``ck_irene_briefing_status_vocabulary``.
BRIEFING_STATUSES: tuple[str, ...] = ("written", "skipped")

#: Why a beat wrote no briefing — mirrors
#: ``ck_irene_briefing_skip_reason_vocabulary``.
BRIEFING_SKIP_REASONS: tuple[str, ...] = ("llm_error", "invalid_output", "nothing_to_write")


class IreneBriefing(Base):
    """One append-only briefing row of one beat under a tenant context."""

    __tablename__ = "irene_briefing"
    __table_args__ = (
        CheckConstraint(
            "status IN ('written', 'skipped')",
            name="ck_irene_briefing_status_vocabulary",
        ),
        CheckConstraint(
            "skip_reason IS NULL OR skip_reason IN ('llm_error', 'invalid_output', "
            "'nothing_to_write')",
            name="ck_irene_briefing_skip_reason_vocabulary",
        ),
        CheckConstraint(
            "(status = 'skipped') = (skip_reason IS NOT NULL)",
            name="ck_irene_briefing_skip_reason_iff_skipped",
        ),
        CheckConstraint(
            "(status = 'written' AND text_markets IS NOT NULL AND text_watch IS NOT NULL) "
            "OR (status <> 'written' AND text_markets IS NULL AND text_watch IS NULL)",
            name="ck_irene_briefing_texts_iff_written",
        ),
        UniqueConstraint(
            "tenant_id",
            "beat_at",
            name="uq_irene_briefing_tenant_beat_at",
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
    beat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Set by the application, not by the insert's transaction time.
    written_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    skip_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    text_markets: Mapped[str | None] = mapped_column(Text, nullable=True)
    text_watch: Mapped[str | None] = mapped_column(Text, nullable=True)
    inputs: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # Plain references to the findings the beat appended; no FK.
    finding_ids: Mapped[list[UUID]] = mapped_column(
        PG_ARRAY(PG_UUID(as_uuid=True)),
        nullable=False,
    )
    model_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
