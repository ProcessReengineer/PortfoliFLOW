# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""b014_super_admin_audit_nullable_actor

Relaxes ``super_admin_audit.super_admin_user_id`` from NOT NULL to
NULL. The bootstrap path (creating the very first super-admin) has
no acting super-admin to attribute the row to; self-attribution
(the workaround in :mod:`services.super_admin.operations`) is
semantically wrong — "the new user created themselves" misrepresents
the audit trail.

After this migration, the ``create_super_admin_idempotent`` helper's
self-attribution path is removed (see the matching code change in
``services/super_admin/operations.py``). Existing self-attributed
rows from bootstraps that ran on b013 are left untouched — they are
imprecise but not corrupt.

Refs: ADR-0064 §4

Revision ID: b014_super_admin_audit_nullable_actor
Revises: b013_super_admin_audit
Create Date: 2026-05-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision = "b014_sa_audit_nullable_actor"  # 28 Zeichen
down_revision = "b013_super_admin_audit"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "super_admin_audit",
        "super_admin_user_id",
        existing_type=sa.dialects.postgresql.UUID(),
        nullable=True,
    )


def downgrade() -> None:
    # Downgrade is unsafe — there may be NULL rows from the bootstrap
    # path that the NOT NULL constraint would reject. Operators who
    # need to downgrade must manually decide how to handle the NULL
    # rows (delete them, attribute them, etc.).
    raise RuntimeError(
        "Downgrade of b014 requires manual handling of NULL "
        "super_admin_user_id rows. Refusing to auto-downgrade."
    )
