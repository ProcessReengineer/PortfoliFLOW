# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Add Irene persistence layer: watch_state, finding, schedule.

Revision ID: b019_add_irene_persistence
Revises: b018_fix_anlv_category_labels
Create Date: 2026-07-02 12:00:00 UTC

The persistence layer for Feature #033 (Decision Console / Irene), per
ADR-0085. Three tenant-scoped tables land together because they form
the three distinct storage concerns of the heartbeat, with different
write patterns and retention semantics:

1. ``irene_watch_state`` — typed world state, upserted once per beat
   per monitored subject. Identity ``(tenant_id, subject_key)``.
   ``magnitude`` is nullable (non-scalar subjects); the
   ``acknowledged_*`` pair records the state the user has already seen
   and is diffed against by the delta logic (ADR-0086).
2. ``irene_finding`` — append-only findings / journal. ``subject_key``
   is a plain reference, NOT an FK to ``irene_watch_state`` (findings
   outlive state rows). ``payload`` carries the ``surface_finding``
   contract (ADR-0088). No ``updated_at``: findings are immutable
   except for the resolution fields.
3. ``irene_schedule`` — per-tenant cadence configuration. ``user_id``
   is nullable and present from day one (per-user seam) but unused in
   v0. The ``(tenant_id, user_id)`` unique constraint does not prevent
   duplicate ``(tenant_id, NULL)`` rows (NULLs are distinct in a
   Postgres unique index); v0 upholds tenant-level uniqueness in the
   repository, deliberately leaving the per-user seam loose (ADR-0085).

All three are tenant-scoped (per ADR-0035): ``tenant_id`` is required
on every row and the standard ``apply_tenant_rls(...)`` policy is
applied to each table, so RLS is enforced through the application-role
switch established in ADR-0078. No SQL enums are used for the ``band`` /
``resolution`` / ``cadence`` vocabularies — the canonical values are
enforced in application code and tests, matching the codebase's
TEXT-for-status convention.

The migration is fully reversible: ``downgrade`` drops the three tables
in reverse dependency order. Postgres drops each table's RLS policy and
row-security state together with the table, so no explicit policy drop
is required (ADR-0085 §Deliverables).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b019_add_irene_persistence"
down_revision: str | None = "b018_fix_anlv_category_labels"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# ---------------------------------------------------------------------------
# upgrade
# ---------------------------------------------------------------------------


def upgrade() -> None:
    # ---- irene_watch_state ------------------------------------------------
    op.create_table(
        "irene_watch_state",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("subject_key", sa.Text(), nullable=False),
        # Nullable: non-scalar subjects have no single measured magnitude.
        sa.Column("magnitude", sa.Numeric(), nullable=True),
        sa.Column("band", sa.Text(), nullable=True),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acknowledged_magnitude", sa.Numeric(), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "subject_key",
            name="uq_irene_watch_state_tenant_subject",
        ),
    )

    op.execute("SELECT apply_tenant_rls('irene_watch_state');")

    # ---- irene_finding ----------------------------------------------------
    op.create_table(
        "irene_finding",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        # NOT an FK to irene_watch_state: findings outlive state rows.
        sa.Column("subject_key", sa.Text(), nullable=False),
        sa.Column("payload", sa.dialects.postgresql.JSONB, nullable=False),
        sa.Column("urgency", sa.SmallInteger(), nullable=False),
        sa.Column("band", sa.Text(), nullable=False),
        sa.Column(
            "resolution",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'open'"),
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        # A user id when known; no FK required.
        sa.Column(
            "resolved_by",
            sa.dialects.postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
    )

    op.execute("SELECT apply_tenant_rls('irene_finding');")

    # ---- irene_schedule ---------------------------------------------------
    op.create_table(
        "irene_schedule",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        # Per-user seam: nullable and present from day one, unused in v0.
        sa.Column(
            "user_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column("cadence", sa.Text(), nullable=False),
        sa.Column("preferred_hour", sa.SmallInteger(), nullable=True),
        sa.Column("timezone", sa.Text(), nullable=False),
        sa.Column(
            "enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column("next_due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_beat_at", sa.DateTime(timezone=True), nullable=True),
        # Reserved for per-tenant event-trigger selection (v1); empty in v0.
        sa.Column(
            "event_profile",
            sa.dialects.postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        # NULLs are distinct in a Postgres unique index, so this does NOT
        # prevent duplicate (tenant_id, NULL) rows. v0 upholds tenant-level
        # uniqueness in the repository; the per-user seam is left loose
        # deliberately (ADR-0085).
        sa.UniqueConstraint(
            "tenant_id",
            "user_id",
            name="uq_irene_schedule_tenant_user",
        ),
    )

    op.execute("SELECT apply_tenant_rls('irene_schedule');")


# ---------------------------------------------------------------------------
# downgrade
# ---------------------------------------------------------------------------


def downgrade() -> None:
    # Reverse creation order. The three tables have no inter-dependencies
    # (each references only tenants). Postgres drops each table's RLS
    # policy and row-security state together with the table, so no
    # explicit policy drop is required.
    op.drop_table("irene_schedule")
    op.drop_table("irene_finding")
    op.drop_table("irene_watch_state")
