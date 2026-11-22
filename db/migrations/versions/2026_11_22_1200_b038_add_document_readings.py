# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Add document_readings and reading_purges, the Reading Desk's store.

Revision ID: b038_add_document_readings
Revises: b037_add_case_working_file
Create Date: 2026-11-22 12:00:00 UTC

The persistence layer of ADR-0139 ("Reading Desk"). A reading is the
structured record of one document, written once when the document is read
and kept without the document itself (§1, §2). This revision lands two
tables:

1. ``reading_purges``: one row per retention run (§5) with its criterion,
   years, cutoff, count and actor. It is created first, because readings
   reference it.
2. ``document_readings``: one row per document read, numbered per tenant.
   Its columns are the list of §2, with no ``created_at`` / ``updated_at``
   beside ``read_at`` and no column for the document or its text. Removing
   a reading purges the three payload columns (``findings``, ``note``,
   ``search_text``) and leaves the tombstone (§5).

Both tables carry the standard ``apply_tenant_rls`` policy.

Two shape CHECKs guard ``document_readings``.
``ck_document_readings_one_assignment`` allows at most one of investment
and manager name. It deliberately leaves ``assigned_by`` / ``assigned_at``
alone: ``ON DELETE SET NULL`` clears ``investment_id`` and keeps them, so
tying them to it would make every deletion of an assigned investment fail.
``ck_document_readings_tombstone_shape`` says that a live reading carries
all three payload columns and no removal field, and that a tombstone
carries none of the three and says who removed it and by which reason or
run. The vocabularies (``origin``, ``document_type``, ``trust`` and those
inside the JSON) are **not** CHECKs: they are TEXT, enforced in the
repository, matching the codebase's convention.

**The audit exception (ADR-0140).** ``reading_purges`` carries the generic
``audit_trigger_function()``. ``document_readings`` does not: that
function captures *full row images* (``to_jsonb(NEW)`` / ``to_jsonb(OLD)``,
no column exclusion), so it would keep in ``audit_log`` exactly what a
tombstone purges. The table carries
``audit_trigger_function_excluding('findings', 'note', 'search_text')``
instead, created here with the generic body plus one block that subtracts
the named columns from both images; everything else, the attribution
included, is audited as before. No other table uses it. The generic
function is not touched, so a later change to it has to be mirrored in
this one. Nothing edits ``audit_log``.

``b035`` stays reserved for ``engagements`` (ADR-0129); this revision
follows b037.

Fully reversible: ``downgrade`` drops ``document_readings``, then
``reading_purges``, then ``audit_trigger_function_excluding``. Postgres
drops each table's RLS policy, row-security state, CHECKs, indexes and
trigger together with the table (the b031 / b033 idiom).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b038_add_document_readings"
down_revision: str | None = "b037_add_case_working_file"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The body of audit_trigger_function() (initial schema) with one inserted
# block (ADR-0140 Decision 1). Every other line is the generic function's,
# verbatim; the b038 round-trip guard compares the two sources.
AUDIT_TRIGGER_FUNCTION_EXCLUDING = """
CREATE FUNCTION audit_trigger_function_excluding()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    affected_row JSONB;
    affected_id  UUID;
    old_payload  JSONB;
    new_payload  JSONB;
    affected_tenant UUID;
    actor_user   UUID;
BEGIN
    IF TG_OP = 'DELETE' THEN
        affected_row := to_jsonb(OLD);
        old_payload  := affected_row;
        new_payload  := NULL;
    ELSIF TG_OP = 'UPDATE' THEN
        affected_row := to_jsonb(NEW);
        old_payload  := to_jsonb(OLD);
        new_payload  := affected_row;
    ELSE  -- INSERT
        affected_row := to_jsonb(NEW);
        old_payload  := NULL;
        new_payload  := affected_row;
    END IF;

    -- The one difference from audit_trigger_function() (ADR-0140): the column
    -- names passed as trigger arguments are subtracted from both row images.
    -- A NULL image stays NULL.
    FOR i IN 0 .. TG_NARGS - 1 LOOP
        old_payload := old_payload - TG_ARGV[i];
        new_payload := new_payload - TG_ARGV[i];
    END LOOP;

    affected_id := (affected_row ->> 'id')::uuid;
    affected_tenant := (affected_row ->> 'tenant_id')::uuid;

    -- Phase 1: app.user_id is not set; Phase 2 will populate it from
    -- the auth-middleware. current_setting(..., true) returns NULL
    -- when the GUC is missing instead of raising.
    BEGIN
        actor_user := NULLIF(current_setting('app.user_id', true), '')::uuid;
    EXCEPTION WHEN others THEN
        actor_user := NULL;
    END;

    INSERT INTO audit_log (
        tenant_id, user_id, table_name, record_id, operation,
        old_data, new_data
    ) VALUES (
        affected_tenant, actor_user, TG_TABLE_NAME, affected_id, TG_OP,
        old_payload, new_payload
    );

    RETURN NULL;  -- AFTER triggers ignore the return value.
END;
$$;
"""

DROP_AUDIT_TRIGGER_FUNCTION_EXCLUDING = "DROP FUNCTION audit_trigger_function_excluding();"

# A live reading carries the three payload columns and no removal field; a
# tombstone carries none of the three and says who, when and by what.
_TOMBSTONE_SHAPE = """
(removed_at IS NULL AND removed_by IS NULL AND removal_reason IS NULL AND purge_id IS NULL
 AND findings IS NOT NULL AND note IS NOT NULL AND search_text IS NOT NULL)
OR
(removed_at IS NOT NULL AND removed_by IS NOT NULL
 AND (removal_reason IS NOT NULL OR purge_id IS NOT NULL)
 AND findings IS NULL AND note IS NULL AND search_text IS NULL)
"""


def _uuid() -> sa.dialects.postgresql.UUID:
    return sa.dialects.postgresql.UUID(as_uuid=True)


def _id_and_tenant() -> list[sa.Column]:
    """``id`` and ``tenant_id``: the head of both tables."""
    return [
        sa.Column(
            "id",
            _uuid(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            _uuid(),
            sa.ForeignKey("tenants.id", ondelete="RESTRICT"),
            nullable=False,
        ),
    ]


# ---------------------------------------------------------------------------
# upgrade
# ---------------------------------------------------------------------------


def upgrade() -> None:
    op.execute(AUDIT_TRIGGER_FUNCTION_EXCLUDING)

    # ---- reading_purges ---------------------------------------------------
    op.create_table(
        "reading_purges",
        *_id_and_tenant(),
        sa.Column("criterion", sa.Text(), nullable=False),
        sa.Column("years", sa.Integer(), nullable=False),
        sa.Column("cutoff", sa.Date(), nullable=False),
        sa.Column("reading_count", sa.Integer(), nullable=False),
        sa.Column(
            "run_by",
            _uuid(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("run_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.execute("SELECT apply_tenant_rls('reading_purges');")
    op.execute(
        """
        CREATE TRIGGER reading_purges_audit_trigger
        AFTER INSERT OR UPDATE OR DELETE ON reading_purges
        FOR EACH ROW
        EXECUTE FUNCTION audit_trigger_function();
        """
    )

    # ---- document_readings ------------------------------------------------
    op.create_table(
        "document_readings",
        # --- identity ------------------------------------------------------
        *_id_and_tenant(),
        # Per tenant, never reused: a tombstone keeps its number.
        sa.Column("reading_number", sa.Integer(), nullable=False),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        # As reported by the model; there is no PDF parser.
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "read_by",
            _uuid(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("origin", sa.Text(), nullable=False, server_default=sa.text("'upload'")),
        sa.Column(
            "case_attachment_id",
            _uuid(),
            sa.ForeignKey("case_attachments.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        # --- classification ------------------------------------------------
        sa.Column("document_type", sa.Text(), nullable=False),
        sa.Column("profile_version", sa.Integer(), nullable=False),
        sa.Column("language", sa.Text(), nullable=True),
        # --- subject -------------------------------------------------------
        sa.Column("fund_name_read", sa.Text(), nullable=True),
        sa.Column("manager_name_read", sa.Text(), nullable=True),
        sa.Column("document_date", sa.Date(), nullable=True),
        sa.Column("period_label", sa.Text(), nullable=True),
        sa.Column("currency", sa.Text(), nullable=True),
        # Deleting the investment leaves the reading unassigned (RD-D-29).
        sa.Column(
            "investment_id",
            _uuid(),
            sa.ForeignKey("investments.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("manager_name", sa.Text(), nullable=True),
        sa.Column(
            "assigned_by",
            _uuid(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=True),
        # --- findings and reading note: nullable only for the tombstone ----
        sa.Column("findings", sa.dialects.postgresql.JSONB, nullable=True),
        sa.Column("note", sa.dialects.postgresql.JSONB, nullable=True),
        # --- provenance ----------------------------------------------------
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("prompt_version", sa.Text(), nullable=False),
        sa.Column(
            "adhoc_keywords",
            sa.dialects.postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("trust", sa.Text(), nullable=False, server_default=sa.text("'untrusted'")),
        # --- search: nullable only for the tombstone -----------------------
        sa.Column("search_text", sa.Text(), nullable=True),
        # --- removal -------------------------------------------------------
        sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "removed_by",
            _uuid(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("removal_reason", sa.Text(), nullable=True),
        sa.Column(
            "purge_id",
            _uuid(),
            sa.ForeignKey("reading_purges.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "reading_number",
            name="uq_document_readings_tenant_reading_number",
        ),
        sa.CheckConstraint(
            "investment_id IS NULL OR manager_name IS NULL",
            name="ck_document_readings_one_assignment",
        ),
        sa.CheckConstraint(_TOMBSTONE_SHAPE, name="ck_document_readings_tombstone_shape"),
    )
    op.create_index(
        "ix_document_readings_tenant_sha256",
        "document_readings",
        ["tenant_id", "sha256"],
    )
    op.create_index(
        "ix_document_readings_tenant_investment",
        "document_readings",
        ["tenant_id", "investment_id"],
    )
    op.create_index(
        "ix_document_readings_tenant_document_date",
        "document_readings",
        ["tenant_id", "document_date"],
    )
    op.execute("SELECT apply_tenant_rls('document_readings');")
    # The arguments are the payload columns a tombstone purges (ADR-0140 §2).
    op.execute(
        """
        CREATE TRIGGER document_readings_audit_trigger
        AFTER INSERT OR UPDATE OR DELETE ON document_readings
        FOR EACH ROW
        EXECUTE FUNCTION audit_trigger_function_excluding('findings', 'note', 'search_text');
        """
    )


# ---------------------------------------------------------------------------
# downgrade
# ---------------------------------------------------------------------------


def downgrade() -> None:
    # Postgres drops each table's RLS policy, row-security state, CHECKs,
    # indexes and trigger together with the table (the b031 / b033 idiom).
    # document_readings first, as it references reading_purges; the function
    # last, as it can go only once its trigger is gone.
    op.drop_table("document_readings")
    op.drop_table("reading_purges")
    op.execute(DROP_AUDIT_TRIGGER_FUNCTION_EXCLUDING)
