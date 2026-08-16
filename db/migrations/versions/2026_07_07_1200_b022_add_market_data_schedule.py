# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Add market_data_schedule — per-tenant live-import cadence configuration.

Revision ID: b022_add_market_data_schedule
Revises: b021_add_ingest_origin
Create Date: 2026-07-07 12:00:00 UTC

The fifth implementation slice of Live Data Import (roadmap #036), per
ADR-0093. A live import must be **triggered** on a cadence and/or on
demand; ADR-0093 reuses Irene's trigger topology 1:1 (ADR-0086): a dumb
external systemd tick separated from database-driven due evaluation, with
per-tenant advisory locking and a per-tenant system actor for the writes.

This migration adds the **cadence config table**. It is structurally the
market-data analogue of ``irene_schedule`` (b019 / ADR-0085): a tenant-
scoped row carrying ``enabled``, the cadence fields (``cadence`` /
``preferred_hour`` / ``timezone``), the DB-clock due cursor
``next_due_at``, and the last-run metadata (``last_run_at`` — named for
this domain, mirroring irene_schedule's ``last_beat_at``). ``user_id`` is
nullable and present from day one to draw the per-user seam without a
later schema change, but is unused in v0 (exactly one row per tenant with
``user_id IS NULL``). ``event_profile`` is reserved for the deferred
event-trigger seam (ADR-0093 §Consequences), empty in v0.

Unlike Irene, ``enabled`` defaults to **FALSE**: a freshly provisioned
tenant does not silently start fetching from external providers. The
per-tenant seed row (installed through the ``seed_tenant_defaults`` choke-
point / bootstrap, mirroring ADR-0077 parity) therefore lands disabled;
an owner opts the tenant in from the Admin surface.

Cadence legitimately *is* per-tenant calibration (ADR-0093 §"Per-tenant
cadence in a config table"), so — unlike the Excel-precedence invariant
of ADR-0092 — it correctly lives in a config table. As with
irene_schedule, no SQL enum is used for ``cadence``; the canonical
vocabulary is enforced in application code and tests (the codebase's
TEXT-for-status convention).

Tenant-scoped per ADR-0035: ``tenant_id`` is required and the standard
``apply_tenant_rls(...)`` policy is applied, so RLS is enforced through
the application-role switch of ADR-0078. The ``(tenant_id, user_id)``
unique constraint does not prevent duplicate ``(tenant_id, NULL)`` rows
(NULLs are distinct in a Postgres unique index); v0 upholds tenant-level
uniqueness in the repository's read-then-write upsert, deliberately
leaving the per-user seam loose — exactly the irene_schedule stance.

Fully reversible: ``downgrade`` drops the table. Postgres drops the RLS
policy and row-security state together with the table, so no explicit
policy drop is required (b019 precedent).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b022_add_market_data_schedule"
down_revision: str | None = "b021_add_ingest_origin"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# ---------------------------------------------------------------------------
# upgrade
# ---------------------------------------------------------------------------


def upgrade() -> None:
    op.create_table(
        "market_data_schedule",
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
        # Defaults FALSE: a fresh tenant does not silently start fetching
        # (ADR-0093). Contrast irene_schedule, which defaults TRUE.
        sa.Column(
            "enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("next_due_at", sa.DateTime(timezone=True), nullable=False),
        # Last-run metadata for this domain (mirrors irene_schedule's
        # last_beat_at). NULL until the first successful refresh.
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        # Reserved for per-tenant event-trigger selection (v1); empty in v0
        # (ADR-0093 §Consequences — the seam is drawn now, deferred).
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
        # deliberately (ADR-0093, mirroring ADR-0085).
        sa.UniqueConstraint(
            "tenant_id",
            "user_id",
            name="uq_market_data_schedule_tenant_user",
        ),
    )

    op.execute("SELECT apply_tenant_rls('market_data_schedule');")


# ---------------------------------------------------------------------------
# downgrade
# ---------------------------------------------------------------------------


def downgrade() -> None:
    # Postgres drops the table's RLS policy and row-security state together
    # with the table, so no explicit policy drop is required.
    op.drop_table("market_data_schedule")
