# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Add the case working file: six cases columns and eight working-file tables.

Revision ID: b037_add_case_working_file
Revises: b036_briefing_and_family_scope
Create Date: 2026-11-02 20:00:00 UTC

The persistence layer of ADR-0138 ("Cases — the Working File"). A case now
has two layers: the **record** (``case_entries``, append-only and unchanged
in shape) and the **working file** (mutable while the case is open or
waiting, read-only once closed). This revision lands the working file:

1. ``cases`` gains six columns, in this order: ``wait_until`` (the date of
   a waiting case, ADR-0138 §4), ``owner_user_id`` (the owner, §13 —
   backfilled from ``opened_by``, then NOT NULL), ``counterparty`` (free
   text, §5), ``due_on`` (the case's own due date), ``framing`` (the
   ``question`` / ``task`` presentation flag, §3) and ``updated_at`` (the
   working-file stamp — backfilled as ``COALESCE(closed_at, opened_at)``,
   then NOT NULL with ``NOW()`` as its default). Two indexes come with
   them: ``ix_cases_tenant_owner`` for the *Mine* filter and the partial
   ``ix_cases_wait_until`` (``WHERE state = 'waiting'``) for the wake-up
   scan. ``description`` is reinterpreted as the question's prose, not
   renamed.
2. Eight working-file tables (ADR-0138 Object model): ``case_threads``,
   ``case_kept``, ``case_figures``, ``case_conversations``,
   ``case_points``, ``case_decisions``, ``case_investments`` and
   ``case_proposals``. Each carries a denormalised ``tenant_id``
   (ADR-0035 §3, the b031 idiom), a RESTRICT FK to ``cases`` with an
   ``ix_<table>_case_id`` index, and the standard ``apply_tenant_rls``
   policy. ``case_investments`` is unique per pair; ``case_decisions``
   carries the partial unique index ``uq_case_decisions_one_draft``
   (at most one draft — ``recorded_at IS NULL`` — per case, §14).

No audit triggers are installed (the b031 idiom): the record is the audit
layer, and the working file is work in progress whose decision-time state
is frozen into the record at every milestone. No SQL enums and no
vocabulary CHECKs are used for ``state``, ``framing``, the thread status,
the kept class, ``via``, the figure source, the conversation channel or
the proposal item status — the canonical values are enforced in the
repositories, matching the codebase's TEXT-for-status convention. No
legacy rows are migrated into the new tables (D-CS-M-7).

``b035`` stays reserved for ``engagements`` (ADR-0129); this revision
follows b036.

Fully reversible: ``downgrade`` drops the eight tables in FK-safe order
(``case_kept`` first — it references ``case_threads`` and
``case_attachments`` — then ``case_proposals``, ``case_investments``,
``case_decisions``, ``case_points``, ``case_conversations``,
``case_figures`` and ``case_threads``), then the two ``cases`` indexes,
then the six ``cases`` columns in reverse order. Postgres drops each
table's RLS policy, row-security state, indexes and constraints together
with the table (the b031 / b036 idiom). ``state`` values are not
rewritten on the way down.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b037_add_case_working_file"
down_revision: str | None = "b036_briefing_and_family_scope"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The eight working-file tables, in FK-safe drop order.
_DROP_ORDER: tuple[str, ...] = (
    "case_kept",
    "case_proposals",
    "case_investments",
    "case_decisions",
    "case_points",
    "case_conversations",
    "case_figures",
    "case_threads",
)


def _uuid() -> sa.dialects.postgresql.UUID:
    return sa.dialects.postgresql.UUID(as_uuid=True)


def _common_columns() -> list[sa.Column]:
    """``id``, ``tenant_id`` and ``case_id`` — the head of every new table."""
    return [
        sa.Column(
            "id",
            _uuid(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        # Denormalised from the parent cases row for row-local RLS (ADR-0035 §3).
        sa.Column(
            "tenant_id",
            _uuid(),
            sa.ForeignKey("tenants.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "case_id",
            _uuid(),
            sa.ForeignKey("cases.id", ondelete="RESTRICT"),
            nullable=False,
        ),
    ]


def _created_at() -> sa.Column:
    return sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("NOW()"),
    )


def _finish_table(table: str) -> None:
    """Index the case FK and apply the standard tenant RLS policy."""
    op.create_index(f"ix_{table}_case_id", table, ["case_id"])
    op.execute(f"SELECT apply_tenant_rls('{table}');")


# ---------------------------------------------------------------------------
# upgrade
# ---------------------------------------------------------------------------


def upgrade() -> None:
    # ---- cases: six columns -----------------------------------------------
    op.add_column("cases", sa.Column("wait_until", sa.Date(), nullable=True))
    op.add_column(
        "cases",
        sa.Column(
            "owner_user_id",
            _uuid(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=True,
        ),
    )
    # The owner is initially the opener; opened_by stays as history (§13).
    op.execute("UPDATE cases SET owner_user_id = opened_by")
    op.alter_column("cases", "owner_user_id", nullable=False)
    op.add_column("cases", sa.Column("counterparty", sa.Text(), nullable=True))
    op.add_column("cases", sa.Column("due_on", sa.Date(), nullable=True))
    # question / task — presentation only; vocabulary in the repository.
    op.add_column("cases", sa.Column("framing", sa.Text(), nullable=True))
    op.add_column(
        "cases",
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute("UPDATE cases SET updated_at = COALESCE(closed_at, opened_at)")
    op.alter_column(
        "cases",
        "updated_at",
        nullable=False,
        server_default=sa.text("NOW()"),
    )

    # The Mine filter reads by owner; the wake-up step scans waiting cases.
    op.create_index("ix_cases_tenant_owner", "cases", ["tenant_id", "owner_user_id"])
    op.create_index(
        "ix_cases_wait_until",
        "cases",
        ["wait_until"],
        postgresql_where=sa.text("state = 'waiting'"),
    )

    # ---- case_threads -----------------------------------------------------
    op.create_table(
        "case_threads",
        *_common_columns(),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'open'")),
        sa.Column("prose", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("conclusion", sa.Text(), nullable=True),
        _created_at(),
        sa.Column("concluded_at", sa.DateTime(timezone=True), nullable=True),
        # Soft removal: kept rows keep referencing the thread.
        sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True),
    )
    _finish_table("case_threads")

    # ---- case_kept --------------------------------------------------------
    op.create_table(
        "case_kept",
        *_common_columns(),
        # Null = kept at file level.
        sa.Column(
            "thread_id",
            _uuid(),
            sa.ForeignKey("case_threads.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("artifact", sa.Text(), nullable=False),
        sa.Column("caption", sa.Text(), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        # The class contract; immutable except for generated_document (§9).
        sa.Column(
            "payload",
            sa.dialects.postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("payload_sha256", sa.Text(), nullable=False),
        sa.Column(
            "attachment_id",
            _uuid(),
            sa.ForeignKey("case_attachments.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "kept_by",
            _uuid(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("via", sa.Text(), nullable=False),
        sa.Column("kept_at", sa.DateTime(timezone=True), nullable=False),
        # Soft removal only: frozen snapshots reference kept rows (§2, §9).
        sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True),
        _created_at(),
    )
    _finish_table("case_kept")

    # ---- case_figures -----------------------------------------------------
    op.create_table(
        "case_figures",
        *_common_columns(),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        # Null for book rows, which are read live from source_ref.
        sa.Column("value_text", sa.Text(), nullable=True),
        sa.Column(
            "source_ref",
            sa.dialects.postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("as_of", sa.Text(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        _created_at(),
    )
    _finish_table("case_figures")

    # ---- case_conversations -----------------------------------------------
    op.create_table(
        "case_conversations",
        *_common_columns(),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("held_on", sa.Date(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False, server_default=sa.text("'call'")),
        sa.Column("with_whom", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("what_was_said", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("what_was_promised", sa.Text(), nullable=True),
        sa.Column("duration_minutes", sa.Integer(), nullable=True),
        _created_at(),
    )
    _finish_table("case_conversations")

    # ---- case_points ------------------------------------------------------
    op.create_table(
        "case_points",
        *_common_columns(),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column(
            "owner_user_id",
            _uuid(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("due_on", sa.Date(), nullable=True),
        sa.Column("done_at", sa.DateTime(timezone=True), nullable=True),
        # The ticket created from this point; trade_tickets.case_id is the
        # back-link (ADR-0128 §7).
        sa.Column(
            "ticket_id",
            _uuid(),
            sa.ForeignKey("trade_tickets.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        _created_at(),
    )
    _finish_table("case_points")

    # ---- case_decisions ---------------------------------------------------
    op.create_table(
        "case_decisions",
        *_common_columns(),
        sa.Column(
            "options",
            sa.dialects.postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("chosen_key", sa.Text(), nullable=True),
        sa.Column("rationale", sa.Text(), nullable=True),
        # Free text — the committee, not a user reference.
        sa.Column("decided_by", sa.Text(), nullable=True),
        sa.Column("decided_at", sa.Date(), nullable=True),
        sa.Column(
            "recorded_by",
            _uuid(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        # Null = the draft.
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "record_entry_id",
            _uuid(),
            sa.ForeignKey("case_entries.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        _created_at(),
    )
    _finish_table("case_decisions")
    # At most one draft per case (ADR-0138 §14).
    op.create_index(
        "uq_case_decisions_one_draft",
        "case_decisions",
        ["case_id"],
        unique=True,
        postgresql_where=sa.text("recorded_at IS NULL"),
    )

    # ---- case_investments -------------------------------------------------
    op.create_table(
        "case_investments",
        *_common_columns(),
        sa.Column(
            "investment_id",
            _uuid(),
            sa.ForeignKey("investments.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "linked_by",
            _uuid(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("linked_at", sa.DateTime(timezone=True), nullable=False),
        _created_at(),
        sa.UniqueConstraint(
            "case_id",
            "investment_id",
            name="uq_case_investments_case_investment",
        ),
    )
    _finish_table("case_investments")

    # ---- case_proposals ---------------------------------------------------
    op.create_table(
        "case_proposals",
        *_common_columns(),
        sa.Column(
            "requested_by",
            _uuid(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("request_text", sa.Text(), nullable=False),
        sa.Column("landed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "items",
            sa.dialects.postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        _created_at(),
    )
    _finish_table("case_proposals")


# ---------------------------------------------------------------------------
# downgrade
# ---------------------------------------------------------------------------


def downgrade() -> None:
    # FK-safe order: case_kept first (it references case_threads and
    # case_attachments), case_threads last. Postgres drops each table's RLS
    # policy, row-security state, indexes and constraints with the table.
    for table in _DROP_ORDER:
        op.drop_table(table)

    op.drop_index("ix_cases_wait_until", table_name="cases")
    op.drop_index("ix_cases_tenant_owner", table_name="cases")

    # The six cases columns in reverse order; dropping owner_user_id drops
    # its FK constraint with it.
    op.drop_column("cases", "updated_at")
    op.drop_column("cases", "framing")
    op.drop_column("cases", "due_on")
    op.drop_column("cases", "counterparty")
    op.drop_column("cases", "owner_user_id")
    op.drop_column("cases", "wait_until")
