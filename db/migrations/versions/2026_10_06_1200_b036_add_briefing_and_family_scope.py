# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Add irene_briefing, watch_family_scope and the per-family WARN columns.

Revision ID: b036_briefing_and_family_scope
Revises: b034_add_trade_tickets
Create Date: 2026-10-06 12:00:00 UTC

The persistence substrate for ADR-0133, in three parts:

1. ``irene_briefing`` (ADR-0133 §1.1) — the artefact of one completed
   beat: ``written`` with Irene's two parts (``text_markets``,
   ``text_watch``), or ``skipped`` with a ``skip_reason``; in both cases
   with the ``inputs`` she was handed and the ``finding_ids`` the beat
   appended. One row per tenant and ``beat_at``; append-only. The
   CHECKs pin the shape: the status vocabulary, ``skip_reason`` present
   exactly when skipped and from its vocabulary, both texts present
   exactly when written. ``text_watch = ''`` is a valid written part
   (every family switched off).
2. ``watch_family_scope`` (ADR-0133 §2.1) — one switch per watchpoint
   family, separate from mute. Historised on the ADR-0116 §1 revision
   model: immutable rows keyed ``effective_from``, a change inserts a
   later revision. A family with no revision is **on**; nothing is
   seeded. ``preset`` records which sensitivity preset a revision
   applied (audit only); ``rss`` carries none, as a schema fact
   (``ck_watch_family_scope_rss_has_no_preset``, D-WD-13).
3. Six ``floor_calibration.warn_pct_<family>`` columns (ADR-0133 §3.1)
   — the per-family WARN fraction. NULL inherits the tenant-wide
   ``warn_default_pct``. No ``rss`` column: a cluster subject has no
   scalar magnitude to measure a WARN fraction against (D-WD-13).

Audit trigger: one table yes, one table no
------------------------------------------
``irene_briefing`` carries **no** audit trigger: it is a system
artefact written by the tick with no human actor, and its own row is
the record (the b019 ``irene_finding`` idiom). ``watch_family_scope``
**does**: switching a family off is an owner's decision about what
Irene watches, and the ADR-0116 §1 revision model pairs versioning
(reproducibility) with the trigger (actor attribution).

What the schema deliberately does not check
-------------------------------------------
The value bounds of the WARN columns (``50 < value < 100``) are not
CHECKs. They belong to ``save_calibration_revision``, beside the bounds
of every other calibration value — duplicating them here would fork one
contract across two places (the ADR-0116 §3 reasoning).

``b035`` stays reserved for ``engagements``; this revision therefore
follows b034 directly.

Fully reversible: ``downgrade`` drops the six columns, then both tables;
Postgres drops their RLS policies, row-security state, CHECKs, unique
constraints and the audit trigger with them (the b031/b032/b033 idiom).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b036_briefing_and_family_scope"
down_revision: str | None = "b034_add_trade_tickets"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The six per-family WARN columns, in schema order. No ``rss`` (D-WD-13).
_WARN_COLUMNS: tuple[str, ...] = (
    "warn_pct_saa",
    "warn_pct_anlv",
    "warn_pct_price",
    "warn_pct_fx",
    "warn_pct_freshness",
    "warn_pct_liquidity",
)


# ---------------------------------------------------------------------------
# upgrade
# ---------------------------------------------------------------------------


def upgrade() -> None:
    # ---- irene_briefing ---------------------------------------------------
    op.create_table(
        "irene_briefing",
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
        sa.Column("beat_at", sa.DateTime(timezone=True), nullable=False),
        # Set by the application: the instant the beat finished writing, not
        # the insert's transaction time.
        sa.Column("written_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("skip_reason", sa.Text(), nullable=True),
        sa.Column("text_markets", sa.Text(), nullable=True),
        sa.Column("text_watch", sa.Text(), nullable=True),
        sa.Column("inputs", sa.dialects.postgresql.JSONB(), nullable=False),
        # Plain references to the findings the beat appended — no FK, as
        # irene_finding.subject_key is a reference, not a key.
        sa.Column(
            "finding_ids",
            sa.dialects.postgresql.ARRAY(sa.dialects.postgresql.UUID(as_uuid=True)),
            nullable=False,
        ),
        sa.Column("model_id", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.CheckConstraint(
            "status IN ('written', 'skipped')",
            name="ck_irene_briefing_status_vocabulary",
        ),
        sa.CheckConstraint(
            "skip_reason IS NULL OR skip_reason IN ('llm_error', 'invalid_output', "
            "'nothing_to_write')",
            name="ck_irene_briefing_skip_reason_vocabulary",
        ),
        sa.CheckConstraint(
            "(status = 'skipped') = (skip_reason IS NOT NULL)",
            name="ck_irene_briefing_skip_reason_iff_skipped",
        ),
        sa.CheckConstraint(
            "(status = 'written' AND text_markets IS NOT NULL AND text_watch IS NOT NULL) "
            "OR (status <> 'written' AND text_markets IS NULL AND text_watch IS NULL)",
            name="ck_irene_briefing_texts_iff_written",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "beat_at",
            name="uq_irene_briefing_tenant_beat_at",
        ),
    )

    # RLS only — no audit trigger: a system artefact with no human actor.
    op.execute("SELECT apply_tenant_rls('irene_briefing');")

    # ---- watch_family_scope -----------------------------------------------
    op.create_table(
        "watch_family_scope",
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
        # Closed set, CHECK-enforced; the watchpoint family vocabulary.
        sa.Column("family", sa.Text(), nullable=False),
        sa.Column("effective_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        # The preset this revision applied — audit only, never read for
        # display. 'custom' is never stored (D-WD-21).
        sa.Column("preset", sa.Text(), nullable=True),
        sa.Column(
            "changed_by",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.CheckConstraint(
            "family IN ('saa', 'anlv', 'rss', 'price', 'fx', 'freshness', 'liquidity')",
            name="ck_watch_family_scope_family_vocabulary",
        ),
        sa.CheckConstraint(
            "preset IS NULL OR preset IN ('quiet', 'standard', 'nervous')",
            name="ck_watch_family_scope_preset_vocabulary",
        ),
        sa.CheckConstraint(
            "family <> 'rss' OR preset IS NULL",
            name="ck_watch_family_scope_rss_has_no_preset",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "family",
            "effective_from",
            name="uq_watch_family_scope_tenant_family_effective_from",
        ),
    )

    op.execute("SELECT apply_tenant_rls('watch_family_scope');")
    op.execute(
        """
        CREATE TRIGGER watch_family_scope_audit_trigger
        AFTER INSERT OR UPDATE OR DELETE ON watch_family_scope
        FOR EACH ROW
        EXECUTE FUNCTION audit_trigger_function();
        """
    )

    # ---- floor_calibration: per-family WARN -------------------------------
    # NULL inherits the tenant-wide WARN default, warn_default_pct
    # (ADR-0133 §3.1). No rss column: no scalar magnitude (D-WD-13).
    for column in _WARN_COLUMNS:
        op.add_column(
            "floor_calibration",
            sa.Column(column, sa.Numeric(6, 3), nullable=True),
        )


# ---------------------------------------------------------------------------
# downgrade
# ---------------------------------------------------------------------------


def downgrade() -> None:
    for column in reversed(_WARN_COLUMNS):
        op.drop_column("floor_calibration", column)
    # Postgres drops each table's RLS policy, row-security state, CHECKs,
    # unique constraint and audit trigger together with the table, so no
    # explicit drops are required (the b031 / b032 / b033 idiom).
    op.drop_table("watch_family_scope")
    op.drop_table("irene_briefing")
