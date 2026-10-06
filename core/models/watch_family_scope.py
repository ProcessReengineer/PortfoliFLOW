# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""WatchFamilyScope ORM model — whether Irene watches a family at all.

Backs the ``watch_family_scope`` table introduced in migration b036 (per
ADR-0133 §2). **One switch per family** for the seven watchpoint
families, per tenant — separate from mute, which stays a per-subject
layer on the watchpoint.

Historised on the ADR-0116 §1 revision model, like ``floor_calibration``:
one row is one immutable revision of one family's switch, keyed
``(tenant_id, family, effective_from)``; the switch in force is the
latest ``effective_from <= as_of``, and a change inserts a later revision
rather than rewriting one. The generic audit trigger is attached and
``changed_by`` names the actor: switching a family off is an owner's
decision about what Irene watches, and it gets both reproducibility and
attribution.

A family with no revision is **on**, and nothing is seeded. That default
belongs to the resolution, not to this table or its repository.

``preset`` records which sensitivity preset a revision applied — an
audit trail, never read for display; ``custom`` is never stored. ``rss``
(Press coverage) carries no preset, as a schema fact: it has no scalar
magnitude a preset could set.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from core.models.base import Base

# The family vocabulary is imported, never restated: the scope switches the
# same seven families the watchpoints carry. Re-exported so the scope's
# whole vocabulary (families, presets) reads from this one module.
from core.models.watchpoint import WATCHPOINT_FAMILIES as WATCHPOINT_FAMILIES

#: The sensitivity presets a scope revision may record — mirrors
#: ``ck_watch_family_scope_preset_vocabulary``.
SCOPE_PRESETS: tuple[str, ...] = ("quiet", "standard", "nervous")

#: The families that carry no preset — mirrors
#: ``ck_watch_family_scope_rss_has_no_preset``.
PRESETLESS_FAMILIES: tuple[str, ...] = ("rss",)


class WatchFamilyScope(Base):
    """One immutable revision of one tenant's switch for one family."""

    __tablename__ = "watch_family_scope"
    __table_args__ = (
        CheckConstraint(
            "family IN ('saa', 'anlv', 'rss', 'price', 'fx', 'freshness', 'liquidity')",
            name="ck_watch_family_scope_family_vocabulary",
        ),
        CheckConstraint(
            "preset IS NULL OR preset IN ('quiet', 'standard', 'nervous')",
            name="ck_watch_family_scope_preset_vocabulary",
        ),
        CheckConstraint(
            "family <> 'rss' OR preset IS NULL",
            name="ck_watch_family_scope_rss_has_no_preset",
        ),
        UniqueConstraint(
            "tenant_id",
            "family",
            "effective_from",
            name="uq_watch_family_scope_tenant_family_effective_from",
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
    family: Mapped[str] = mapped_column(Text, nullable=False)
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    # Audit only; never read for display. 'custom' is never stored.
    preset: Mapped[str | None] = mapped_column(Text, nullable=True)
    changed_by: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
