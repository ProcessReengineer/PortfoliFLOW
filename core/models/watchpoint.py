# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Watchpoint ORM model — the Watch Desk's historised subject registry.

Backs the ``watchpoints`` table introduced in migration b033 (per
ADR-0116 §1). One row is **one version** of one watchpoint: the stable
identity is ``watchpoint_id``, and versions are keyed
``(tenant_id, watchpoint_id, effective_from)``. The current version is
the latest ``effective_from <= as_of``; an edit inserts a new version and
never updates in place, following the ``limit_sets`` historisation
pattern (ADR-0056). Retirement is a version with ``retired = True``, so
the identity and its history stay queryable and a past finding remains
explainable.

Two shapes in one table, and the difference is load-bearing
-----------------------------------------------------------
For the **derived** families (``saa``, ``anlv``, ``rss``) a watchpoint is
a *sensitivity overlay only*: the subject is enumerated from the
effective limit sets or the ``_KNOWN_TAGS`` vocabulary, and only
``muted`` / ``warn_threshold_pct`` / ``re_trigger_delta`` may be set
(``rss``: ``muted`` alone). Subject identity and ceilings remain solely
with the limit set — there is never a second edit point for limits.

For the four **defined** signal families (``price``, ``fx``,
``freshness``, ``liquidity``) the watchpoint *defines* the subject, and
its family-specific parameter columns must be present.

That asymmetry is enforced by per-family CHECK constraints in the
database, mirrored here in ``__table_args__`` so the ORM metadata stays
a faithful description of the schema. The constraints are the last line
of the guarantee: a repository or UI bug cannot talk the schema into
accepting an overlay row that defines a ceiling.

Value *bounds* (``50 < warn_threshold_pct < 100``, positive deltas and
windows, well-formed ``currency_pair``, and the ``freshness`` /
``liquidity`` singleton rule) are deliberately not modelled here —
ADR-0116 §3 places them in the repository write path and the route, and
a second copy in the schema would fork one contract across two places.

No ``relationship()`` traversals are declared: repositories join
explicitly, per the Phase-3/4 convention.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from core.models.base import Base

#: The closed family vocabulary (ADR-0116 §1), mirrored from the b033
#: CHECK for application-side reference. Ordered overlay families first,
#: then the defined signal families. There is no ``pacing`` member: the TA
#: engine does not exist yet, so plan-deviation watching has no reliable
#: reference object (ADR-0116 Non-goals).
WATCHPOINT_FAMILIES: tuple[str, ...] = (
    "saa",
    "anlv",
    "rss",
    "price",
    "fx",
    "freshness",
    "liquidity",
)

#: The families whose watchpoints are a sensitivity overlay over a subject
#: that is derived elsewhere — never a definition of one.
OVERLAY_FAMILIES: tuple[str, ...] = ("saa", "anlv", "rss")

#: The families whose watchpoint defines the subject it watches.
DEFINED_FAMILIES: tuple[str, ...] = ("price", "fx", "freshness", "liquidity")

#: The families that carry at most **one** identity per tenant: the
#: parameters apply to every investment (``freshness``) or to the book as a
#: whole (``liquidity``), so a second identity would be two answers to one
#: question. Enforced in the repository (ADR-0116 §4).
SINGLETON_FAMILIES: tuple[str, ...] = ("freshness", "liquidity")

#: The eight family-specific parameter columns, in schema order. Named
#: once so the CHECK expressions below read against one list.
_DEFINING_COLUMNS: tuple[str, ...] = (
    "instrument_id",
    "currency_pair",
    "drop_pct",
    "move_pct",
    "window_days",
    "max_age_days",
    "horizon_months",
    "min_coverage_ratio",
)


def _all_null_except(*present: str) -> str:
    """Render ``a IS NULL AND b IS NULL AND …`` over the defining columns."""
    return " AND ".join(
        f"{column} IS NULL" for column in _DEFINING_COLUMNS if column not in present
    )


def _family_shape(family: str, *required: str) -> str:
    """Render one defined family's implication CHECK (b033's expression)."""
    required_clause = " AND ".join(f"{column} IS NOT NULL" for column in required)
    return f"family <> '{family}' OR ({required_clause} AND {_all_null_except(*required)})"


class Watchpoint(Base):
    """One immutable version of one tenant-scoped watchpoint."""

    __tablename__ = "watchpoints"
    __table_args__ = (
        CheckConstraint(
            "family IN ('saa', 'anlv', 'rss', 'price', 'fx', 'freshness', 'liquidity')",
            name="ck_watchpoints_family_vocabulary",
        ),
        CheckConstraint(
            f"family NOT IN ('saa', 'anlv') OR ({_all_null_except()})",
            name="ck_watchpoints_overlay_family_defines_nothing",
        ),
        CheckConstraint(
            f"family <> 'rss' OR ({_all_null_except()} "
            "AND warn_threshold_pct IS NULL AND re_trigger_delta IS NULL)",
            name="ck_watchpoints_rss_carries_mute_only",
        ),
        CheckConstraint(
            _family_shape("price", "instrument_id", "drop_pct", "window_days"),
            name="ck_watchpoints_price_shape",
        ),
        CheckConstraint(
            _family_shape("fx", "currency_pair", "move_pct", "window_days"),
            name="ck_watchpoints_fx_shape",
        ),
        CheckConstraint(
            _family_shape("freshness", "max_age_days"),
            name="ck_watchpoints_freshness_shape",
        ),
        CheckConstraint(
            _family_shape("liquidity", "horizon_months", "min_coverage_ratio"),
            name="ck_watchpoints_liquidity_shape",
        ),
        UniqueConstraint(
            "tenant_id",
            "watchpoint_id",
            "effective_from",
            name="uq_watchpoints_tenant_identity_effective_from",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=text("gen_random_uuid()"),
    )
    watchpoint_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="RESTRICT"),
        nullable=False,
    )
    effective_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    retired: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("FALSE"),
    )
    family: Mapped[str] = mapped_column(Text, nullable=False)
    subject_key: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)

    # --- sensitivity columns ------------------------------------------------
    muted: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("FALSE"),
    )
    warn_threshold_pct: Mapped[Decimal | None] = mapped_column(Numeric(6, 3), nullable=True)
    re_trigger_delta: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)

    # --- defining columns ---------------------------------------------------
    instrument_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("investments.id", ondelete="CASCADE"),
        nullable=True,
    )
    currency_pair: Mapped[str | None] = mapped_column(Text, nullable=True)
    drop_pct: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    move_pct: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    window_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_age_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    horizon_months: Mapped[int | None] = mapped_column(Integer, nullable=True)
    min_coverage_ratio: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

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
