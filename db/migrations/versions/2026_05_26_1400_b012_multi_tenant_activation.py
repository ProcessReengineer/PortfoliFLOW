# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Multi-tenant activation — subdomain, roles, is_super_admin.

Revision ID: b012_multi_tenant_activation
Revises: b011_add_benchmarks
Create Date: 2026-05-26 14:00:00 UTC

Schema substrate for ADR-0063 (Multi-Tenant Activation Phase 1) and
ADR-0064 (Super-Admin Surface). The migration:

1. Adds ``tenants.subdomain`` and ``tenants.is_active`` so the
   subdomain-based ``SubdomainTenantResolver`` can map a request's
   ``Host`` header to a tenant id.
2. Seeds the system tenant (``00000000-0000-0000-0000-000000000000``,
   subdomain ``admin``) and renames the previous "Sentinel Tenant"
   row to "Minathena Capital" with subdomain
   ``minathena-capital`` — the demo identity for the
   structurally-anchored primary tenant.
3. Replaces ``users.is_tenant_owner: BOOLEAN`` with
   ``users.roles: TEXT[]`` (CHECK-constrained to
   ``{'owner', 'member', 'auditor'}``) per ADR-0063 §2 and adds
   ``users.is_super_admin: BOOLEAN`` (CHECK-bound to the system
   tenant) per ADR-0063 §3.

The migration commits the operator stance from ADR-0063 §6: the
project carries test data only, so no row-level data is migrated
beyond the seed-tenant rename. Operators perform a clean re-bootstrap
after applying the migration.

See ADR-0063 §6 and ADR-0064 §5 for the full rationale.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "b012_multi_tenant_activation"
down_revision: str | None = "b011_add_benchmarks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Anchor copies of ``core.tenant_constants``. Migrations stay
# importable without the application package on path; a regression
# test pins the literals against the Python constants.
_PRIMARY_TENANT_ID: str = "00000000-0000-0000-0000-000000000001"
_PRIMARY_TENANT_NAME: str = "Minathena Capital"
_PRIMARY_TENANT_SUBDOMAIN: str = "minathena-capital"

_SYSTEM_TENANT_ID: str = "00000000-0000-0000-0000-000000000000"
_SYSTEM_TENANT_NAME: str = "Platform Administration"
_SYSTEM_TENANT_SUBDOMAIN: str = "admin"


def upgrade() -> None:
    # ------------------------------------------------------------------
    # tenants.subdomain — added nullable so the partial unique index
    # can be installed before the seed rows are populated. NOT NULL
    # set in step 6 once both seed rows carry a subdomain value.
    # ------------------------------------------------------------------
    op.add_column(
        "tenants",
        sa.Column("subdomain", sa.Text(), nullable=True),
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_tenants_subdomain_partial
            ON tenants(subdomain)
            WHERE subdomain IS NOT NULL;
        """
    )

    # ------------------------------------------------------------------
    # Seed the system tenant idempotently. Postgres superuser bypasses
    # the FORCE ROW LEVEL SECURITY on ``tenants`` so the INSERT runs
    # without an ``app.tenant_id`` GUC (same pattern as b008). The id
    # is cast to UUID inside SQL because asyncpg binds the parameter
    # as VARCHAR and the column type is uuid.
    # ------------------------------------------------------------------
    op.execute(
        f"""
        INSERT INTO tenants (id, name, subdomain)
        VALUES (
            '{_SYSTEM_TENANT_ID}'::uuid,
            '{_SYSTEM_TENANT_NAME}',
            '{_SYSTEM_TENANT_SUBDOMAIN}'
        )
        ON CONFLICT (id) DO NOTHING;
        """
    )

    # ------------------------------------------------------------------
    # Rename the sentinel tenant to its production identity. The UUID
    # stays the same — ``PRIMARY_TENANT_ID`` is the new name for the
    # constant previously called ``SENTINEL_TENANT_ID``.
    # ------------------------------------------------------------------
    op.execute(
        f"""
        UPDATE tenants
           SET name = '{_PRIMARY_TENANT_NAME}',
               subdomain = '{_PRIMARY_TENANT_SUBDOMAIN}'
         WHERE id = '{_PRIMARY_TENANT_ID}'::uuid;
        """
    )

    # ------------------------------------------------------------------
    # subdomain NOT NULL + permanent unique index. The partial-fill
    # window above closes here: every existing tenant row now carries
    # a subdomain, and any future INSERT must too.
    # ------------------------------------------------------------------
    op.execute("DROP INDEX uq_tenants_subdomain_partial;")
    op.alter_column("tenants", "subdomain", nullable=False)
    op.create_index(
        "uq_tenants_subdomain",
        "tenants",
        ["subdomain"],
        unique=True,
    )

    # ------------------------------------------------------------------
    # tenants.is_active — used by the deactivation flow in ADR-0064 §1.
    # ------------------------------------------------------------------
    op.add_column(
        "tenants",
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("TRUE"),
        ),
    )

    # ------------------------------------------------------------------
    # users.roles TEXT[] — the role substrate per ADR-0063 §2.
    # Default ``ARRAY['member']`` makes new rows minimum-privilege.
    # The CHECK restricts values to the canonical set and requires at
    # least one role per row (no empty arrays).
    # ------------------------------------------------------------------
    op.execute(
        """
        ALTER TABLE users
        ADD COLUMN roles TEXT[] NOT NULL DEFAULT ARRAY['member']::text[];
        """
    )
    op.execute(
        """
        ALTER TABLE users
        ADD CONSTRAINT ck_users_roles_values CHECK (
            array_length(roles, 1) >= 1
            AND roles <@ ARRAY['owner', 'member', 'auditor']::text[]
        );
        """
    )

    # ------------------------------------------------------------------
    # Backfill ``users.roles`` from ``is_tenant_owner`` before dropping
    # the column. Owners become ``['owner']``; everyone else keeps the
    # default ``['member']`` from the column add above.
    # ------------------------------------------------------------------
    op.execute(
        """
        UPDATE users
           SET roles = ARRAY['owner']::text[]
         WHERE is_tenant_owner = TRUE;
        """
    )

    op.drop_column("users", "is_tenant_owner")

    # ------------------------------------------------------------------
    # users.is_super_admin BOOLEAN — the platform role axis per
    # ADR-0063 §3. The CHECK constraint structurally binds
    # ``is_super_admin = TRUE`` to ``tenant_id = SYSTEM_TENANT_ID``.
    # ------------------------------------------------------------------
    op.add_column(
        "users",
        sa.Column(
            "is_super_admin",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("FALSE"),
        ),
    )
    op.execute(
        f"""
        ALTER TABLE users
        ADD CONSTRAINT ck_users_super_admin_in_system_tenant CHECK (
            is_super_admin = FALSE
            OR tenant_id = '{_SYSTEM_TENANT_ID}'::uuid
        );
        """
    )


def downgrade() -> None:
    # Per project convention (b010), downgrade is intentionally a
    # no-op. The db-reset workflow re-applies from scratch; a
    # production-grade downgrade is not part of the operator
    # commitment.
    pass
