# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Seed the default development tenant row.

Revision ID: b008_seed_default_tenant
Revises: b007_add_sector_country_weights
Create Date: 2026-05-11 03:57:00 UTC

The Phase-2 auth backend resolves every email to
``SENTINEL_TENANT_ID`` (``core/tenant_constants.py``) until real
multi-tenant resolution lands in Phase 6 (P6-A). Every authenticated
write — including the ``login_audit`` row that records the
attempt — references that UUID through a foreign key.

The initial schema migration (b001) creates the ``tenants`` table
but deliberately does not insert any rows: at that point in the
Phase-1 plan there was no auth backend that would need it. Through
Phases 1–3 the dev database was hand-seeded by the local-dev
bootstrap workflow; that step had no audit trail and did not
survive a Postgres container reset. As a result, the very first
login attempt after a container reset failed with
``ForeignKeyViolationError`` on ``login_audit.tenant_id_fkey``.

This migration closes the gap. It inserts a single sentinel-tenant
row using ``INSERT ... ON CONFLICT (id) DO NOTHING`` so it is safe
to re-run after either a partial application or a hand-applied
fixup. The row is owned by no user and has no business meaning
beyond serving as the anchor every Phase-2 foreign key resolves
against. Block 2's real multi-tenant work will replace this with a
proper tenant-management surface — at that point this seed becomes
the development-only fixture it always implicitly was.

Postgres superuser bypasses the ``FORCE ROW LEVEL SECURITY`` on
``tenants`` (the ``tenant_self_visibility`` policy from b001 would
otherwise reject an INSERT without a matching ``app.tenant_id``
GUC), so this migration runs cleanly under the Alembic env's
``DATABASE_URL_SUPERUSER`` connection.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "b008_seed_default_tenant"
down_revision: str | None = "b007_add_sector_country_weights"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Anchor copy of ``core.tenant_constants.SENTINEL_TENANT_ID``.
# Migrations must be importable without the application package on
# the path, so the UUID is duplicated rather than imported. A
# regression test pins the two values together.
_SENTINEL_TENANT_ID: str = "00000000-0000-0000-0000-000000000001"
_SENTINEL_TENANT_NAME: str = "Sentinel Tenant"


def upgrade() -> None:
    tenants = sa.table(
        "tenants",
        sa.column("id", sa.dialects.postgresql.UUID(as_uuid=True)),
        sa.column("name", sa.String(255)),
    )
    insert_stmt = sa.dialects.postgresql.insert(tenants).values(
        [{"id": _SENTINEL_TENANT_ID, "name": _SENTINEL_TENANT_NAME}]
    )
    op.execute(insert_stmt.on_conflict_do_nothing(index_elements=["id"]))


def downgrade() -> None:
    # Deleting the sentinel row is only safe when no FK-bearing row
    # still references it. The ``ondelete="RESTRICT"`` on every
    # tenant-scoped table will fail the DELETE loudly if Phase-2+
    # data is still present — which is exactly the safety net we
    # want. A real teardown belongs to the operator, not to a
    # downgrade hook.
    op.execute(sa.text("DELETE FROM tenants WHERE id = :id").bindparams(id=_SENTINEL_TENANT_ID))
