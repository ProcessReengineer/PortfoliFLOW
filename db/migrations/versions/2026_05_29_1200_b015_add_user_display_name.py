# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""b015_add_user_display_name

Adds a nullable ``users.display_name TEXT`` column. The ``users`` table
previously carried only ``email`` as an identifier — there was no human
name to greet an operator by. ADR-0068 introduces the Front Office
welcome header (``Welcome back, {first name} — {tenant} portfolio``),
which derives the first name from this column.

The column is **nullable and optional**: every user-creation path
threads an optional ``display_name`` (default ``None``), so no caller is
forced to supply it and existing rows need no backfill (the project has
no production tenants yet). ``email``, ``password_hash`` and the RLS
policies are untouched.

Refs: ADR-0068

Revision ID: b015_add_user_display_name
Revises: b014_sa_audit_nullable_actor
Create Date: 2026-05-29
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision = "b015_add_user_display_name"
down_revision = "b014_sa_audit_nullable_actor"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("display_name", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "display_name")
